"""Read model: one symbol's latest consensus and its revision (hk 6508). Read-only.

``build_consensus_view`` is pure. ``read_latest_consensus`` only SELECTs. MCP
exposure is a follow-up; nothing here is registered as a tool.

Revision semantics. For a row the page gives its columns in the header order,
e.g. for the consensus chart ``VAL1`` = the current value and ``VAL2``..``VAL5``
= the value 1 month, 3 months, 6 months and 1 year earlier. Labels are the
page's own text (``column_label``) and are not reinterpreted. ``vs_previous_day``
compares ``VAL1`` with the latest earlier stored consensus snapshot and reports
that snapshot's date, so a gap in collection stays visible. Deltas are exact
decimal strings; percentage is derived and rounded to 4 decimal places.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fnguide import FnguideSnapshot, FnguideValue

REVISION_ITEMS: tuple[str, ...] = ("목표주가", "EPS")
_VAL_RE = re.compile(r"VAL(\d+)")
_PCT_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class SnapshotMeta:
    symbol: str
    observed_date: date
    fetched_at: datetime
    source_url: str
    company_name: str | None
    status: str
    reason: str | None


@dataclass(frozen=True)
class ValueMeta:
    item_name: str
    unit: str | None
    digit: int | None
    column_key: str
    column_label: str | None
    value_text: str | None
    value_reason: str | None


@dataclass(frozen=True)
class PreviousDay:
    observed_date: date
    values: Sequence[ValueMeta]


def _col_order(key: str) -> tuple[int, str]:
    match = _VAL_RE.fullmatch(key)
    return (int(match.group(1)), key) if match else (10**6, key)


def _delta(current: str, base: str) -> dict[str, str | None]:
    diff = Decimal(current) - Decimal(base)
    if Decimal(base) == 0:
        pct: str | None = None
        pct_reason: str | None = "zero_base"
    else:
        pct = format((diff / Decimal(base) * 100).quantize(_PCT_QUANT), "f")
        pct_reason = None
    return {
        "delta": format(diff, "f"),
        "delta_pct": pct,
        "delta_pct_reason": pct_reason,
    }


def _cell(value: ValueMeta) -> dict[str, Any]:
    return {
        "column_key": value.column_key,
        "column_label": value.column_label,
        "value": value.value_text,
        "reason": value.value_reason,
    }


def _item_cells(values: Sequence[ValueMeta], item: str) -> dict[str, ValueMeta]:
    return {v.column_key: v for v in values if v.item_name == item}


def _revision(
    item: str, values: Sequence[ValueMeta], previous: PreviousDay | None
) -> dict[str, Any]:
    cells = _item_cells(values, item)
    current = cells.get("VAL1")
    current_text = current.value_text if current else None
    result: dict[str, Any] = {
        "current": {
            "column_label": current.column_label if current else None,
            "value": current_text,
            "reason": (current.value_reason if current else "column_absent"),
        },
        "vs_earlier": [],
        "vs_previous_day": None,
    }
    for key in sorted((k for k in cells if k != "VAL1"), key=_col_order):
        if not _VAL_RE.fullmatch(key):
            continue
        earlier = cells[key]
        entry: dict[str, Any] = {
            "column_key": key,
            "column_label": earlier.column_label,
            "earlier_value": earlier.value_text,
            "delta": None,
            "delta_pct": None,
            "reason": None,
        }
        if current_text is None:
            entry["reason"] = "current_missing"
        elif earlier.value_text is None:
            entry["reason"] = earlier.value_reason or "missing"
        else:
            entry.update(_delta(current_text, earlier.value_text))
        result["vs_earlier"].append(entry)

    if previous is not None:
        prev_cell = _item_cells(previous.values, item).get("VAL1")
        prev_entry: dict[str, Any] = {
            "observed_date": previous.observed_date.isoformat(),
            "value": prev_cell.value_text if prev_cell else None,
            "delta": None,
            "delta_pct": None,
            "reason": None,
        }
        if current_text is None:
            prev_entry["reason"] = "current_missing"
        elif prev_cell is None or prev_cell.value_text is None:
            prev_entry["reason"] = (
                prev_cell.value_reason if prev_cell else "column_absent"
            ) or "missing"
        else:
            prev_entry.update(_delta(current_text, prev_cell.value_text))
        result["vs_previous_day"] = prev_entry
    return result


def build_consensus_view(
    snapshot: SnapshotMeta,
    values: Sequence[ValueMeta],
    previous: PreviousDay | None,
) -> dict[str, Any]:
    items: dict[str, dict[str, Any]] = {}
    for value in sorted(values, key=lambda v: (v.item_name, _col_order(v.column_key))):
        item = items.setdefault(
            value.item_name,
            {
                "item": value.item_name,
                "unit": value.unit,
                "digit": value.digit,
                "columns": [],
            },
        )
        item["columns"].append(_cell(value))
    has_values = bool(items)
    revision = (
        {item: _revision(item, values, previous) for item in REVISION_ITEMS}
        if has_values
        else {}
    )
    return {
        "symbol": snapshot.symbol,
        "observed_date": snapshot.observed_date.isoformat(),
        "fetched_at": snapshot.fetched_at.isoformat(),
        "source_url": snapshot.source_url,
        "company_name": snapshot.company_name,
        "status": snapshot.status,
        "reason": snapshot.reason,
        "items": list(items.values()),
        "revision": revision,
    }


def _to_value_meta(row: FnguideValue) -> ValueMeta:
    return ValueMeta(
        item_name=row.item_name,
        unit=row.unit,
        digit=row.digit,
        column_key=row.column_key,
        column_label=row.column_label,
        value_text=row.value_text,
        value_reason=row.value_reason,
    )


async def read_latest_consensus(
    session: AsyncSession, symbol: str
) -> dict[str, Any] | None:
    """Latest stored Consensus snapshot for ``symbol`` with its revision, or None."""
    snap = (
        await session.execute(
            select(FnguideSnapshot)
            .where(
                FnguideSnapshot.symbol == symbol, FnguideSnapshot.page == "Consensus"
            )
            .order_by(FnguideSnapshot.observed_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snap is None:
        return None
    rows = (
        (
            await session.execute(
                select(FnguideValue).where(
                    FnguideValue.snapshot_id == snap.id,
                    FnguideValue.section == "consensus",
                )
            )
        )
        .scalars()
        .all()
    )
    previous: PreviousDay | None = None
    prev_snap = (
        await session.execute(
            select(FnguideSnapshot)
            .where(
                FnguideSnapshot.symbol == symbol,
                FnguideSnapshot.page == "Consensus",
                FnguideSnapshot.status == "parsed",
                FnguideSnapshot.observed_date < snap.observed_date,
            )
            .order_by(FnguideSnapshot.observed_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prev_snap is not None:
        prev_rows = (
            (
                await session.execute(
                    select(FnguideValue).where(
                        FnguideValue.snapshot_id == prev_snap.id,
                        FnguideValue.section == "consensus",
                        FnguideValue.item_name.in_(REVISION_ITEMS),
                        FnguideValue.column_key == "VAL1",
                    )
                )
            )
            .scalars()
            .all()
        )
        previous = PreviousDay(
            observed_date=prev_snap.observed_date,
            values=[_to_value_meta(r) for r in prev_rows],
        )
    meta = SnapshotMeta(
        symbol=snap.symbol,
        observed_date=snap.observed_date,
        fetched_at=snap.fetched_at,
        source_url=snap.source_url,
        company_name=snap.company_name,
        status=snap.status,
        reason=snap.reason,
    )
    return build_consensus_view(meta, [_to_value_meta(r) for r in rows], previous)


__all__ = [
    "REVISION_ITEMS",
    "PreviousDay",
    "SnapshotMeta",
    "ValueMeta",
    "build_consensus_view",
    "read_latest_consensus",
]
