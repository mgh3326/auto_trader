"""Read-only Alpaca Paper execution anomaly checks (ROB-93).

The checks in this module are deterministic and side-effect free.  They do not
call brokers, submit/cancel orders, repair rows, or write to the database.  A
runner or report builder supplies ledger rows plus optional read-only broker
snapshots, and the service returns an operator-readable preflight report.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

STALE_PREVIEW_CLEANUP_REQUIRED_STATE = "stale_preview_cleanup_required"
STALE_PREVIEW_CLEANUP_ACTION = "mark_stale_preview_cleanup_required"


class PaperExecutionAnomalySeverity(StrEnum):
    """Severity used by runner gates and operator reports."""

    info = "info"
    warning = "warning"
    block = "block"


@dataclass(frozen=True)
class PaperExecutionAnomaly:
    """One deterministic anomaly finding."""

    check_id: str
    severity: PaperExecutionAnomalySeverity
    summary: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": self.severity.value,
            "summary": self.summary,
            "details": self.details,
        }


@dataclass(frozen=True)
class PaperExecutionPreflightReport:
    """Preflight anomaly report consumed by runners and audit views."""

    status: str
    should_block: bool
    checked_at: datetime
    stale_after_minutes: int
    anomalies: tuple[PaperExecutionAnomaly, ...]
    counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "should_block": self.should_block,
            "checked_at": self.checked_at.isoformat(),
            "stale_after_minutes": self.stale_after_minutes,
            "counts": self.counts,
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


_OPEN_LEDGER_STATES = frozenset({"submitted", "open", "partially_filled"})
_CANONICAL_FILLED_LEDGER_STATES = frozenset(
    {"filled", "position_reconciled", "closed", "final_reconciled"}
)
_FILLED_STATES = frozenset({"filled", "partially_filled"})
_BUY_REQUIRES_LINKED_SELL_STATES = _FILLED_STATES | _CANONICAL_FILLED_LEDGER_STATES
_TERMINAL_STATES = frozenset({"filled", "canceled"})
_SELL_SOURCE_KEYS = frozenset(
    {
        "source_client_order_id",
        "source_order_client_order_id",
        "previous_buy_client_order_id",
        "buy_client_order_id",
        "source_ledger_client_order_id",
    }
)


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").upper().replace("/", "").replace("-", "").strip()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iter_nested_values(payload: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key), value
            yield from _iter_nested_values(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_nested_values(item)


def _source_client_order_ids(row: Any) -> set[str]:
    ids: set[str] = set()
    for field in ("preview_payload", "validation_summary", "raw_responses"):
        for key, value in _iter_nested_values(_get(row, field) or {}):
            if key in _SELL_SOURCE_KEYS and value:
                ids.add(str(value))
    return ids


def _packet_value(packet: Any, key: str) -> Any:
    if isinstance(packet, dict):
        return packet.get(key)
    return getattr(packet, key, None)


def _scope_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _preflight_scope_from_packet(packet: Any) -> dict[str, str]:
    """Extract stable per-order/session scope keys from an approval packet.

    ``lifecycle_correlation_id`` is the preferred ledger scope. Candidate and
    briefing UUIDs cover preopen/decision-session provenance, and
    ``client_order_id`` keeps duplicate/idempotency checks tied to the packet.
    """
    scope: dict[str, str] = {}
    for key in (
        "lifecycle_correlation_id",
        "candidate_uuid",
        "briefing_artifact_run_uuid",
        "artifact_id",
    ):
        value = _scope_value(_packet_value(packet, key))
        if value:
            scope[key] = value

    # A packet's new client_order_id alone is not enough to narrow historical
    # ledger checks; otherwise a broad preflight with a candidate packet would
    # accidentally skip unrelated open round-trip anomalies. Once a stable
    # correlation/session/provenance key exists, include the client ID as an
    # additional match key for mixed old/new ledger rows.
    client_order_id = _scope_value(_packet_value(packet, "client_order_id"))
    if scope and client_order_id:
        scope["client_order_id"] = client_order_id
    return scope


def _row_matches_preflight_scope(row: Any, scope: dict[str, str]) -> bool:
    """Return whether a ledger row belongs to the selected packet scope.

    The matching is intentionally OR-based across stable provenance keys because
    older rows may have only client_order_id/correlation while newer rows may
    carry candidate or briefing UUID provenance.
    """
    if not scope:
        return True

    checks = {
        "lifecycle_correlation_id": _scope_value(_get(row, "lifecycle_correlation_id")),
        "client_order_id": _scope_value(_get(row, "client_order_id")),
        "candidate_uuid": _scope_value(_get(row, "candidate_uuid")),
        "briefing_artifact_run_uuid": _scope_value(
            _get(row, "briefing_artifact_run_uuid")
        ),
    }
    # The packet artifact_id is commonly the same value as
    # briefing_artifact_run_uuid for preopen approval packets.
    if (
        scope.get("artifact_id")
        and checks.get("briefing_artifact_run_uuid") == scope["artifact_id"]
    ):
        return True

    return any(checks.get(key) == value for key, value in scope.items())


def _is_open_order(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "").lower()
    if not status:
        return True
    return status not in {"filled", "canceled", "cancelled", "expired", "rejected"}


def _position_qty(position: dict[str, Any]) -> Decimal:
    return _decimal(
        position.get("qty")
        or position.get("quantity")
        or position.get("position_qty")
        or position.get("available")
    )


def _latest_preview_time(row: Any) -> datetime | None:
    candidates = [
        _get(row, "approval_bridge_generated_at"),
        _get(row, "created_at"),
    ]
    preview_payload = _get(row, "preview_payload") or {}
    if isinstance(preview_payload, dict):
        candidates.extend(
            [
                preview_payload.get("generated_at"),
                preview_payload.get("previewed_at"),
                preview_payload.get("expires_at"),
            ]
        )
    for value in candidates:
        parsed = _as_aware_utc(_parse_datetime(value))
        if parsed is not None:
            return parsed
    return None


def _row_ref(row: Any) -> dict[str, Any]:
    return {
        "client_order_id": _get(row, "client_order_id"),
        "lifecycle_correlation_id": _get(row, "lifecycle_correlation_id"),
        "candidate_uuid": _get(row, "candidate_uuid"),
        "briefing_artifact_run_uuid": _get(row, "briefing_artifact_run_uuid"),
        "side": _get(row, "side"),
        "lifecycle_state": _get(row, "lifecycle_state"),
        "order_status": _get(row, "order_status"),
        "execution_symbol": _get(row, "execution_symbol"),
        "signal_symbol": _get(row, "signal_symbol"),
        "filled_qty": str(_get(row, "filled_qty"))
        if _get(row, "filled_qty") is not None
        else None,
        "created_at": _iso(_get(row, "created_at")),
    }


def _cleanup_required_row_ref(row: Any) -> dict[str, Any]:
    ref = _row_ref(row)
    ref["recommended_lifecycle_state"] = STALE_PREVIEW_CLEANUP_REQUIRED_STATE
    ref["recommended_action"] = STALE_PREVIEW_CLEANUP_ACTION
    return ref


def build_paper_execution_preflight_report(
    *,
    ledger_rows: Iterable[Any] = (),
    open_orders: Iterable[dict[str, Any]] = (),
    positions: Iterable[dict[str, Any]] = (),
    approval_packet: dict[str, Any] | None = None,
    expected_signal_symbol: str | None = None,
    expected_execution_symbol: str | None = None,
    now: datetime | None = None,
    stale_after_minutes: int = 30,
) -> PaperExecutionPreflightReport:
    """Build a read-only Alpaca Paper execution preflight anomaly report.

    Args:
        ledger_rows: Recent or correlation-scoped ledger rows. ORM rows and
            dictionaries are both accepted for deterministic tests.
        open_orders: Read-only broker open-order snapshot already fetched by
            the caller. Non-terminal rows block a new cycle.
        positions: Read-only position snapshot already fetched by the caller.
            Any non-zero quantity blocks a new cycle.
        approval_packet: Optional preview/approval packet being considered for
            execution. Used for duplicate, stale, and symbol checks.
        expected_signal_symbol: Optional symbol from the signal artifact.
        expected_execution_symbol: Optional symbol expected at Alpaca Paper.
        now: Clock injection for deterministic tests.
        stale_after_minutes: Preview/approval max age before blocking.
    """
    checked_at = _as_aware_utc(now) or datetime.now(UTC)
    unscoped_ledger = list(ledger_rows)
    orders = list(open_orders)
    position_rows = list(positions)
    packet = approval_packet or {}
    preflight_scope = _preflight_scope_from_packet(packet)
    ledger = [
        row
        for row in unscoped_ledger
        if _row_matches_preflight_scope(row, preflight_scope)
    ]
    anomalies: list[PaperExecutionAnomaly] = []

    def add(
        check_id: str,
        severity: PaperExecutionAnomalySeverity,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        anomalies.append(PaperExecutionAnomaly(check_id, severity, summary, details))

    # 1. Unexpected open orders.
    open_snapshot = [o for o in orders if _is_open_order(o)]
    if open_snapshot:
        add(
            "unexpected_open_orders",
            PaperExecutionAnomalySeverity.block,
            "Alpaca Paper has open orders before starting a new cycle",
            {
                "count": len(open_snapshot),
                "orders": [
                    {
                        "id": o.get("id") or o.get("order_id"),
                        "client_order_id": o.get("client_order_id"),
                        "symbol": o.get("symbol"),
                        "status": o.get("status"),
                        "side": o.get("side"),
                    }
                    for o in open_snapshot
                ],
            },
        )

    # 2. Residual positions before a new cycle.
    residual_positions = [p for p in position_rows if _position_qty(p) != Decimal("0")]
    if residual_positions:
        add(
            "residual_position_exists",
            PaperExecutionAnomalySeverity.block,
            "Residual Alpaca Paper position exists before starting a new cycle",
            {
                "count": len(residual_positions),
                "positions": [
                    {
                        "symbol": p.get("symbol"),
                        "qty": str(_position_qty(p)),
                        "asset_class": p.get("asset_class"),
                    }
                    for p in residual_positions
                ],
            },
        )

    # 3. Duplicate client_order_id, both within the ledger slice and against
    # the candidate approval packet.
    by_client_id: dict[str, list[Any]] = {}
    for row in ledger:
        client_id = str(_get(row, "client_order_id") or "").strip()
        if client_id:
            by_client_id.setdefault(client_id, []).append(row)
    duplicate_ids = {k: v for k, v in by_client_id.items() if len(v) > 1}
    packet_client_id = str(packet.get("client_order_id") or "").strip()
    if packet_client_id and packet_client_id in by_client_id:
        duplicate_ids.setdefault(packet_client_id, by_client_id[packet_client_id])
    if duplicate_ids:
        add(
            "duplicate_client_order_id",
            PaperExecutionAnomalySeverity.block,
            "client_order_id is already present in the Alpaca Paper ledger",
            {
                "client_order_ids": sorted(duplicate_ids),
                "rows": [
                    _row_ref(row) for rows in duplicate_ids.values() for row in rows[:5]
                ],
            },
        )

    # 4. Previous buy filled but no linked sell exists.
    sell_source_ids = {
        source_id
        for row in ledger
        if str(_get(row, "side") or "").lower() == "sell"
        for source_id in _source_client_order_ids(row)
    }
    filled_buys_missing_sell = []
    for row in ledger:
        side = str(_get(row, "side") or "").lower()
        state = str(_get(row, "lifecycle_state") or "").lower()
        client_id = str(_get(row, "client_order_id") or "").strip()
        if (
            side == "buy"
            and state in _BUY_REQUIRES_LINKED_SELL_STATES
            and client_id not in sell_source_ids
        ):
            filled_buys_missing_sell.append(row)
    if filled_buys_missing_sell:
        add(
            "previous_buy_filled_sell_missing",
            PaperExecutionAnomalySeverity.block,
            "A previous filled buy has no linked sell ledger row",
            {"rows": [_row_ref(r) for r in filled_buys_missing_sell[:10]]},
        )

    # 5. Sell filled but final position not closed.
    sells_not_closed = []
    for row in ledger:
        side = str(_get(row, "side") or "").lower()
        state = str(_get(row, "lifecycle_state") or "").lower()
        if side != "sell" or state != "filled":
            continue
        snapshot = _get(row, "position_snapshot") or {}
        if not isinstance(snapshot, dict) or _position_qty(snapshot) != Decimal("0"):
            sells_not_closed.append(row)
    if sells_not_closed:
        add(
            "sell_filled_position_not_closed",
            PaperExecutionAnomalySeverity.block,
            "A filled sell does not have a zero final position snapshot",
            {"rows": [_row_ref(r) for r in sells_not_closed[:10]]},
        )

    # 6. Ledger/order/fill mismatches.
    mismatches = []
    for row in ledger:
        state = str(_get(row, "lifecycle_state") or "").lower()
        order_status = str(_get(row, "order_status") or "").lower()
        filled_qty = _decimal(_get(row, "filled_qty"))
        if state == "filled" and filled_qty <= Decimal("0"):
            mismatches.append(
                {"reason": "filled_state_without_filled_qty", **_row_ref(row)}
            )
        if (
            order_status == "filled"
            and state
            and state not in _CANONICAL_FILLED_LEDGER_STATES
        ):
            mismatches.append(
                {"reason": "order_status_filled_state_mismatch", **_row_ref(row)}
            )
        if state in _OPEN_LEDGER_STATES and order_status in {"filled", "canceled"}:
            mismatches.append(
                {"reason": "terminal_order_status_with_open_state", **_row_ref(row)}
            )
    if mismatches:
        add(
            "ledger_order_fill_mismatch",
            PaperExecutionAnomalySeverity.block,
            "Ledger lifecycle state does not match order/fill data",
            {"rows": mismatches[:10]},
        )

    # 7. Stale preview/approval packet.
    stale_cutoff = checked_at - timedelta(minutes=stale_after_minutes)
    stale_rows = []
    for row in ledger:
        state = str(_get(row, "lifecycle_state") or "").lower()
        if state not in {"previewed", "validation_failed"}:
            continue
        preview_time = _latest_preview_time(row)
        if preview_time is not None and preview_time < stale_cutoff:
            stale_rows.append(row)
    packet_time = _as_aware_utc(
        _parse_datetime(
            packet.get("expires_at")
            or packet.get("generated_at")
            or packet.get("approval_bridge_generated_at")
        )
    )
    if packet_time is not None and packet_time < stale_cutoff:
        stale_rows.append(
            {"client_order_id": packet_client_id, "created_at": packet_time}
        )
    if stale_rows:
        add(
            "stale_preview_or_approval_packet",
            PaperExecutionAnomalySeverity.block,
            "Preview or approval packet is older than the allowed threshold",
            {
                "stale_after_minutes": stale_after_minutes,
                "cutoff": stale_cutoff.isoformat(),
                "lifecycle_state": STALE_PREVIEW_CLEANUP_REQUIRED_STATE,
                "recommended_lifecycle_state": STALE_PREVIEW_CLEANUP_REQUIRED_STATE,
                "recommended_action": STALE_PREVIEW_CLEANUP_ACTION,
                "cleanup_plan": {
                    "mode": "dry_run",
                    "mutates_broker": False,
                    "mutates_db": False,
                    "description": (
                        "Mark same-scope stale preview rows cleanup-required only "
                        "through a separately approved cleanup operation."
                    ),
                },
                "rows": [_cleanup_required_row_ref(r) for r in stale_rows[:10]],
            },
        )

    # 8. Signal/execution symbol mismatch.
    symbol_mismatches = []
    expected_signal = _normalize_symbol(
        expected_signal_symbol or packet.get("signal_symbol")
    )
    expected_execution = _normalize_symbol(
        expected_execution_symbol or packet.get("execution_symbol")
    )
    for row in ledger:
        row_signal = _normalize_symbol(_get(row, "signal_symbol"))
        row_execution = _normalize_symbol(_get(row, "execution_symbol"))
        if expected_signal and row_signal and row_signal != expected_signal:
            symbol_mismatches.append(
                {
                    "reason": "signal_symbol_mismatch",
                    "expected": expected_signal_symbol or packet.get("signal_symbol"),
                    **_row_ref(row),
                }
            )
        if expected_execution and row_execution and row_execution != expected_execution:
            symbol_mismatches.append(
                {
                    "reason": "execution_symbol_mismatch",
                    "expected": expected_execution_symbol
                    or packet.get("execution_symbol"),
                    **_row_ref(row),
                }
            )
    if symbol_mismatches:
        add(
            "signal_execution_symbol_mismatch",
            PaperExecutionAnomalySeverity.block,
            "Signal or execution symbol does not match the approval context",
            {"rows": symbol_mismatches[:10]},
        )

    # Info row for a clean preflight gives operators an explicit positive audit
    # marker without weakening block semantics.
    if not anomalies:
        add(
            "preflight_clean",
            PaperExecutionAnomalySeverity.info,
            "No Alpaca Paper execution anomalies detected",
            {},
        )

    should_block = any(
        a.severity == PaperExecutionAnomalySeverity.block for a in anomalies
    )
    return PaperExecutionPreflightReport(
        status="blocked" if should_block else "pass",
        should_block=should_block,
        checked_at=checked_at,
        stale_after_minutes=stale_after_minutes,
        anomalies=tuple(anomalies),
        counts={
            "ledger_rows": len(ledger),
            "unscoped_ledger_rows": len(unscoped_ledger),
            "open_orders": len(orders),
            "positions": len(position_rows),
            "block": sum(
                a.severity == PaperExecutionAnomalySeverity.block for a in anomalies
            ),
            "warning": sum(
                a.severity == PaperExecutionAnomalySeverity.warning for a in anomalies
            ),
            "info": sum(
                a.severity == PaperExecutionAnomalySeverity.info for a in anomalies
            ),
        },
    )


__all__ = [
    "STALE_PREVIEW_CLEANUP_ACTION",
    "STALE_PREVIEW_CLEANUP_REQUIRED_STATE",
    "PaperExecutionAnomaly",
    "PaperExecutionAnomalySeverity",
    "PaperExecutionPreflightReport",
    "build_paper_execution_preflight_report",
]
