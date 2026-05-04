"""Map KIS websocket parsed-event dicts to ROB-100 OrderLifecycleEvent.

Pure mapping helper. Lives next to the parser/client but does not import them
(only the schema), so it stays reusable from tests and the smoke harness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.execution_contracts import (
    AccountMode,
    OrderLifecycleEvent,
    OrderLifecycleState,
)

_DOMESTIC_FILL_YN_TO_STATE: dict[str, OrderLifecycleState] = {
    "2": "fill",
    "1": "pending",
}

_OVERSEAS_STATUS_TO_STATE: dict[str, OrderLifecycleState] = {
    "filled": "fill",
    "partial": "fill",
    "rejected": "failed",
    "canceled": "failed",
    "accepted": "accepted",
    "order_notice": "accepted",
    "anomaly": "anomaly",
}


def _resolve_state(parsed: dict[str, Any]) -> tuple[OrderLifecycleState, list[str]]:
    warnings: list[str] = []
    market = parsed.get("market")

    if market == "kr":
        fill_yn = str(parsed.get("fill_yn") or "").strip()
        state = _DOMESTIC_FILL_YN_TO_STATE.get(fill_yn)
        if state is None:
            warnings.append(f"unknown_kis_domestic_fill_yn:{fill_yn!r}")
            return "anomaly", warnings
        return state, warnings

    if market == "us":
        status = str(parsed.get("execution_status") or "").strip().lower()
        state = _OVERSEAS_STATUS_TO_STATE.get(status)
        if state is None:
            warnings.append(f"unknown_kis_overseas_status:{status!r}")
            return "anomaly", warnings
        return state, warnings

    warnings.append(f"unknown_kis_market:{market!r}")
    return "anomaly", warnings


def _resolve_occurred_at(parsed: dict[str, Any]) -> datetime:
    for key in ("filled_at", "received_at"):
        raw = parsed.get(key)
        if not raw:
            continue
        try:
            # datetime.fromisoformat might return offset-naive or offset-aware
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value
    return datetime.now(UTC).replace(microsecond=0)


_DETAIL_KEYS = (
    "tr_code",
    "market",
    "symbol",
    "side",
    "filled_price",
    "filled_qty",
    "filled_amount",
    "filled_at",
    "fill_yn",
    "execution_status",
    "cntg_yn",
    "rfus_yn",
    "acpt_yn",
    "rctf_cls",
    "order_qty",
    "currency",
    "received_at",
    "raw_fields_count",
)


def build_lifecycle_event(
    parsed: dict[str, Any],
    *,
    account_mode: AccountMode,
) -> OrderLifecycleEvent:
    """Build a typed ``OrderLifecycleEvent`` from a KIS parsed dict.

    The caller supplies ``account_mode`` (the runtime is authoritative); any
    ``account_mode`` key inside ``parsed`` is ignored.
    """
    if account_mode not in ("kis_live", "kis_mock"):
        raise ValueError(
            f"account_mode must be 'kis_live' or 'kis_mock', got {account_mode!r}"
        )

    state, warnings = _resolve_state(parsed)

    detail: dict[str, Any] = {
        key: parsed[key] for key in _DETAIL_KEYS if key in parsed
    }

    broker_order_id = parsed.get("order_id")
    if broker_order_id is not None:
        broker_order_id = str(broker_order_id)

    correlation_id = parsed.get("correlation_id")
    if correlation_id is not None:
        correlation_id = str(correlation_id)

    return OrderLifecycleEvent(
        account_mode=account_mode,
        execution_source="websocket",
        state=state,
        occurred_at=_resolve_occurred_at(parsed),
        broker_order_id=broker_order_id,
        correlation_id=correlation_id,
        detail=detail,
        warnings=warnings,
    )


__all__ = ["build_lifecycle_event"]
