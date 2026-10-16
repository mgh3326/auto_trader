"""Read the frozen 2026-10-16 input snapshot (read-only).

Authority per field:

* timestamps -> the JSON files (tz-aware). 🔴 ``01_rejected_cohort.csv``'s
  ``created_kst`` column is actually the UTC minute of ``created_at`` (e.g.
  JSON ``2026-10-16T13:44:16Z`` == CSV ``2026-10-16T13:44`` for a session
  labelled ``...-2235`` KST), so it is never used as a clock.
* ``labels`` / ``primary`` -> the v0 classification in the CSV, joined by row
  order after asserting that symbol and the UTC minute agree on every row.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

MARKET_OF_INSTRUMENT = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}
MARKET_OF_CURRENCY = {"KRW": "kr", "USD": "us"}


@dataclass(frozen=True, slots=True)
class Rejection:
    row: int
    forecast_id: str
    market: str
    symbol: str
    created_at: datetime
    session_label: str | None
    policy_version: str | None
    primary: str
    labels: frozenset[str]
    reason: str
    target_kind: str | None
    direction: str | None
    target_price: float | None
    probability: float | None
    forecast_start: date | None
    review_date: date | None
    window_start: date | None  # resolver's scored window (resolution_detail)
    window_end: date | None
    status: str
    outcome: bool | None


@dataclass(frozen=True, slots=True)
class TossOrder:
    order_id: str
    market: str
    symbol: str
    side: str
    status: str
    order_type: str
    price: float | None
    quantity: float
    ordered_at: datetime
    canceled_at: datetime | None
    filled_qty: float
    avg_fill: float | None
    filled_at: datetime | None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def load_rejections(inputs: Path) -> list[Rejection]:
    entries = json.loads((inputs / "forecasts_deferred.json").read_text())["entries"]
    with (inputs / "01_rejected_cohort.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != len(entries):
        raise ValueError(f"cohort CSV {len(rows)} rows != JSON {len(entries)}")
    out: list[Rejection] = []
    for i, (e, r) in enumerate(zip(entries, rows, strict=True)):
        if e["symbol"] != r["symbol"] or e["created_at"][:16] != r["created_kst"]:
            raise ValueError(f"cohort CSV/JSON misaligned at row {i}")
        target = e.get("forecast_target") or {}
        price = target.get("target_price")
        out.append(
            Rejection(
                row=i,
                forecast_id=e["forecast_id"],
                market=MARKET_OF_INSTRUMENT[e["instrument_type"]],
                symbol=e["symbol"],
                created_at=datetime.fromisoformat(e["created_at"]),
                session_label=e.get("session_label"),
                policy_version=e.get("policy_version"),
                primary=r["primary"],
                labels=frozenset(x for x in (r["labels"] or "").split("|") if x),
                reason=e.get("contrary_evidence") or "",
                target_kind=target.get("kind"),
                direction=target.get("direction"),
                target_price=float(price) if price is not None else None,
                probability=e.get("probability"),
                forecast_start=_date(e.get("forecast_start_date")),
                review_date=_date(e.get("review_date")),
                window_start=_date(
                    (e.get("resolution_detail") or {}).get("window_start")
                ),
                window_end=_date((e.get("resolution_detail") or {}).get("window_end")),
                status=e["status"],
                outcome=e.get("outcome"),
            )
        )
    return out


def load_toss_orders(inputs: Path) -> list[TossOrder]:
    payload = json.loads((inputs / "toss_orders.json").read_text())["result"]
    orders = payload if isinstance(payload, list) else payload.get("orders", [])
    out: list[TossOrder] = []
    for o in orders:
        execution = o.get("execution") or {}
        avg = execution.get("averageFilledPrice")
        filled_at = execution.get("filledAt")
        out.append(
            TossOrder(
                order_id=o["order_id"],
                market=MARKET_OF_CURRENCY[o["currency"]],
                symbol=o["symbol"],
                side=o["side"],
                status=o["status"],
                order_type=o.get("order_type") or "",
                price=float(o["price"]) if o.get("price") is not None else None,
                quantity=float(o["quantity"]),
                ordered_at=datetime.fromisoformat(o["ordered_at"]),
                canceled_at=(
                    datetime.fromisoformat(o["canceled_at"])
                    if o.get("canceled_at")
                    else None
                ),
                filled_qty=float(execution.get("filledQuantity") or 0),
                avg_fill=float(avg) if avg else None,
                filled_at=datetime.fromisoformat(filled_at) if filled_at else None,
            )
        )
    return out
