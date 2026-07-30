"""Read-only Alpaca Paper execution anomaly checks (ROB-93).

The checks in this module are deterministic and side-effect free.  They do not
call brokers, submit/cancel orders, repair rows, or write to the database.  A
runner or report builder supplies ledger rows plus optional read-only broker
snapshots, and the service returns an operator-readable preflight report.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

STALE_PREVIEW_CLEANUP_REQUIRED_STATE = "stale_preview_cleanup_required"
STALE_PREVIEW_CLEANUP_ACTION = "mark_stale_preview_cleanup_required"

# ROB-1130: a broker snapshot is only evidence if the caller states when it was
# fetched. Without that, "queried and genuinely empty" and "never queried" arrive
# as the same empty list and the gate cannot tell them apart.
DEFAULT_SNAPSHOT_MAX_AGE_MINUTES = 5
_SNAPSHOT_FUTURE_TOLERANCE = timedelta(seconds=60)
_SNAPSHOT_KINDS = ("positions", "open_orders")


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
    broker_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "should_block": self.should_block,
            "checked_at": self.checked_at.isoformat(),
            "stale_after_minutes": self.stale_after_minutes,
            "counts": self.counts,
            "broker_snapshot": self.broker_snapshot,
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


_OPEN_LEDGER_STATES = frozenset({"submitted", "open", "partially_filled"})
_CANONICAL_FILLED_LEDGER_STATES = frozenset(
    {"filled", "position_reconciled", "closed", "final_reconciled"}
)
_FILLED_STATES = frozenset({"filled", "partially_filled"})
_BUY_REQUIRES_LINKED_SELL_STATES = _FILLED_STATES | _CANONICAL_FILLED_LEDGER_STATES
# States in which a buy leg legitimately holds an open position and therefore
# has no sell leg yet. Reaching one of these states is not evidence that a sell
# is missing; only a contradiction against the live position snapshot is.
_BUY_OPEN_POSITION_STATES = frozenset(
    {"filled", "partially_filled", "position_reconciled"}
)
# States that assert the roundtrip already finished. A completed buy leg with no
# sell row is a genuine missing-leg defect regardless of current holdings.
_BUY_COMPLETED_ROUNDTRIP_STATES = frozenset({"closed", "final_reconciled"})
_TERMINAL_STATES = frozenset({"filled", "canceled"})
_TERMINAL_PREVIEW_SIBLING_STATES = frozenset({"final_reconciled", "closed", "canceled"})
_SELL_SOURCE_KEYS = frozenset(
    {
        "source_buy_client_order_id",
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


def _strict_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return decimal if decimal.is_finite() else None


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
    for field_name in ("preview_payload", "validation_summary", "raw_responses"):
        for key, value in _iter_nested_values(_get(row, field_name) or {}):
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


def _clean_lower(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _snapshot_rows(value: Any) -> list[Any] | None:
    """Row list for a caller-supplied broker snapshot.

    ``None`` means the snapshot was never fetched. A mapping/string container is
    not a row sequence (ROB-1130) — handing over the whole MCP response envelope
    must never let its keys be read as broker rows — so it degrades to an empty
    but unusable list that ``_verify_broker_snapshot`` reports as unverified.
    """
    if value is None:
        return None
    if isinstance(value, (Mapping, str, bytes)):
        return []
    return list(value)


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


def _position_symbol(position: dict[str, Any]) -> str:
    return _normalize_symbol(
        position.get("symbol")
        or position.get("asset_symbol")
        or position.get("execution_symbol")
    )


def _strict_position_qty(position: dict[str, Any]) -> Decimal | None:
    raw_qty = next(
        (
            position[key]
            for key in ("qty", "quantity", "position_qty", "available")
            if key in position and position[key] not in (None, "")
        ),
        None,
    )
    return _strict_decimal(raw_qty)


def _position_snapshot_issues(positions: Iterable[Any]) -> list[dict[str, Any]]:
    """Return standalone schema defects in a caller-supplied position snapshot."""
    issues: list[dict[str, Any]] = []
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            issues.append({"index": index, "reason": "position_row_not_object"})
            continue
        symbol = _position_symbol(position)
        if not symbol:
            issues.append({"index": index, "reason": "position_symbol_missing"})
            continue
        raw_qty = next(
            (
                position[key]
                for key in ("qty", "quantity", "position_qty", "available")
                if key in position and position[key] not in (None, "")
            ),
            None,
        )
        if raw_qty is None:
            issues.append(
                {
                    "index": index,
                    "reason": "position_qty_missing",
                    "symbol": symbol,
                }
            )
            continue
        if _strict_decimal(raw_qty) is None:
            issues.append(
                {
                    "index": index,
                    "reason": "position_qty_invalid",
                    "symbol": symbol,
                }
            )
    return issues


def _position_qty_by_symbol(
    positions: Iterable[Any],
) -> tuple[dict[str, Decimal], set[str]]:
    """Aggregate a broker position snapshot by normalized execution symbol."""
    quantities: dict[str, Decimal] = {}
    invalid_symbols: set[str] = set()
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = _position_symbol(position)
        if not symbol:
            continue
        qty = _strict_position_qty(position)
        if qty is None:
            invalid_symbols.add(symbol)
            continue
        quantities[symbol] = quantities.get(symbol, Decimal("0")) + qty
    return quantities, invalid_symbols


def _ledger_row_broker_symbol(row: Any) -> str:
    """Broker-side symbol for a ledger row.

    ``execution_symbol`` is the symbol the order was actually placed with, so it
    is the only field comparable to a broker position snapshot. ``signal_symbol``
    is only used when the execution symbol is missing, because signal symbols use
    a different namespace for crypto (``KRW-BTC`` vs ``BTCUSD``).
    """
    return _normalize_symbol(_get(row, "execution_symbol")) or _normalize_symbol(
        _get(row, "signal_symbol")
    )


def _ledger_row_created_at(row: Any) -> datetime | None:
    return _as_aware_utc(_parse_datetime(_get(row, "created_at")))


def _is_filled_sell_evidence(row: Any) -> bool:
    """Return whether a ledger row proves that a sell filled at the broker."""
    return (
        str(_get(row, "side") or "").lower() == "sell"
        and str(_get(row, "lifecycle_state") or "").lower()
        in _CANONICAL_FILLED_LEDGER_STATES
        and str(_get(row, "order_status") or "").lower() == "filled"
        and (filled_qty := _strict_decimal(_get(row, "filled_qty"))) is not None
        and filled_qty > Decimal("0")
    )


def _match_filled_buys_to_sells(
    ledger: Iterable[Any],
) -> tuple[set[int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Match filled buys to completed sells without inventing shared identities.

    Current submit paths persist ``source_buy_client_order_id`` on source-bound
    sells. The ROB-1129 historical rows predate that contract: every leg has its
    own correlation/client ID and no source field. For those rows only, use a
    conservative one-to-one fallback requiring the same broker symbol, exactly
    equal positive filled quantity, and a sell timestamp at or after the buy.

    A sell with any explicit source ID is never reassigned by the legacy
    fallback, and each sell can discharge at most one buy.
    """
    rows = list(ledger)
    buys = [
        row
        for row in rows
        if str(_get(row, "side") or "").lower() == "buy"
        and str(_get(row, "lifecycle_state") or "").lower()
        in _BUY_REQUIRES_LINKED_SELL_STATES
    ]
    all_sells = [row for row in rows if str(_get(row, "side") or "").lower() == "sell"]
    sells = [row for row in all_sells if _is_filled_sell_evidence(row)]
    used_sell_tokens: set[int] = set()
    matched_buy_tokens: set[int] = set()
    legacy_matches: list[dict[str, Any]] = []
    source_issues: list[dict[str, Any]] = []

    buys_by_client_id: dict[str, list[Any]] = {}
    for buy in buys:
        client_id = str(_get(buy, "client_order_id") or "").strip()
        if client_id:
            buys_by_client_id.setdefault(client_id, []).append(buy)

    # Strong path: all explicit source evidence is reserved from the legacy
    # fallback. A source buy is complete only when one or more terminal filled
    # sells consume exactly its positive filled quantity after the buy timestamp.
    valid_source_sells: dict[int, list[tuple[Any, Decimal]]] = {}
    source_buys: dict[int, Any] = {}
    invalid_source_buy_tokens: set[int] = set()
    for sell in all_sells:
        source_ids = _source_client_order_ids(sell)
        if not source_ids:
            continue
        used_sell_tokens.add(id(sell))
        if len(source_ids) != 1:
            source_issues.append(
                {
                    "reason": "ambiguous_source_buy_ids",
                    "source_buy_client_order_ids": sorted(source_ids),
                    "sell": _row_ref(sell),
                }
            )
            continue
        source_id = next(iter(source_ids))
        source_candidates = buys_by_client_id.get(source_id, [])
        if len(source_candidates) != 1:
            source_issues.append(
                {
                    "reason": (
                        "source_buy_not_found"
                        if not source_candidates
                        else "source_buy_ambiguous"
                    ),
                    "source_buy_client_order_id": source_id,
                    "sell": _row_ref(sell),
                }
            )
            continue
        buy = source_candidates[0]
        buy_token = id(buy)
        source_buys[buy_token] = buy
        buy_symbol = _ledger_row_broker_symbol(buy)
        sell_symbol = _ledger_row_broker_symbol(sell)
        if not buy_symbol or buy_symbol != sell_symbol:
            invalid_source_buy_tokens.add(buy_token)
            source_issues.append(
                {
                    "reason": "source_sell_symbol_mismatch",
                    "source_buy_client_order_id": source_id,
                    "buy": _row_ref(buy),
                    "sell": _row_ref(sell),
                }
            )
            continue
        buy_created_at = _ledger_row_created_at(buy)
        sell_created_at = _ledger_row_created_at(sell)
        if buy_created_at is None or sell_created_at is None:
            invalid_source_buy_tokens.add(buy_token)
            source_issues.append(
                {
                    "reason": "source_chronology_missing",
                    "source_buy_client_order_id": source_id,
                    "buy": _row_ref(buy),
                    "sell": _row_ref(sell),
                }
            )
            continue
        if sell_created_at < buy_created_at:
            invalid_source_buy_tokens.add(buy_token)
            source_issues.append(
                {
                    "reason": "source_sell_before_buy",
                    "source_buy_client_order_id": source_id,
                    "buy": _row_ref(buy),
                    "sell": _row_ref(sell),
                }
            )
            continue
        sell_qty = _strict_decimal(_get(sell, "filled_qty"))
        if sell_qty is None or sell_qty <= Decimal("0"):
            invalid_source_buy_tokens.add(buy_token)
            source_issues.append(
                {
                    "reason": "source_sell_qty_invalid",
                    "source_buy_client_order_id": source_id,
                    "sell": _row_ref(sell),
                }
            )
            continue
        if not _is_filled_sell_evidence(sell):
            invalid_source_buy_tokens.add(buy_token)
            source_issues.append(
                {
                    "reason": "source_sell_not_terminal_filled",
                    "source_buy_client_order_id": source_id,
                    "sell": _row_ref(sell),
                }
            )
            continue
        valid_source_sells.setdefault(buy_token, []).append((sell, sell_qty))

    for buy_token, buy in source_buys.items():
        buy_qty = _strict_decimal(_get(buy, "filled_qty"))
        if buy_qty is None or buy_qty <= Decimal("0"):
            invalid_source_buy_tokens.add(buy_token)
            source_issues.append(
                {
                    "reason": "source_buy_qty_invalid",
                    "buy": _row_ref(buy),
                }
            )
            continue
        total_sell_qty = sum(
            (qty for _, qty in valid_source_sells.get(buy_token, [])),
            start=Decimal("0"),
        )
        if buy_token in invalid_source_buy_tokens:
            continue
        if total_sell_qty < buy_qty:
            source_issues.append(
                {
                    "reason": "source_sell_qty_incomplete",
                    "buy_filled_qty": str(buy_qty),
                    "source_sell_filled_qty": str(total_sell_qty),
                    "buy": _row_ref(buy),
                }
            )
            continue
        if total_sell_qty > buy_qty:
            source_issues.append(
                {
                    "reason": "source_sell_qty_exceeds_buy",
                    "buy_filled_qty": str(buy_qty),
                    "source_sell_filled_qty": str(total_sell_qty),
                    "buy": _row_ref(buy),
                }
            )
            continue
        matched_buy_tokens.add(buy_token)

    # Legacy path: exact economic identity plus chronology, one sell per buy.
    def _sort_key(row: Any) -> tuple[datetime, str]:
        return (
            _ledger_row_created_at(row) or datetime.max.replace(tzinfo=UTC),
            str(_get(row, "client_order_id") or ""),
        )

    for buy in sorted(buys, key=_sort_key):
        if id(buy) in matched_buy_tokens:
            continue
        buy_symbol = _ledger_row_broker_symbol(buy)
        buy_qty = _strict_decimal(_get(buy, "filled_qty"))
        buy_created_at = _ledger_row_created_at(buy)
        if (
            not buy_symbol
            or buy_qty is None
            or buy_qty <= Decimal("0")
            or buy_created_at is None
        ):
            continue

        candidate = next(
            (
                sell
                for sell in sorted(sells, key=_sort_key)
                if id(sell) not in used_sell_tokens
                and not _source_client_order_ids(sell)
                and _ledger_row_broker_symbol(sell) == buy_symbol
                and _strict_decimal(_get(sell, "filled_qty")) == buy_qty
                and (
                    (sell_created_at := _ledger_row_created_at(sell)) is not None
                    and sell_created_at >= buy_created_at
                )
            ),
            None,
        )
        if candidate is None:
            continue
        matched_buy_tokens.add(id(buy))
        used_sell_tokens.add(id(candidate))
        legacy_matches.append(
            {
                "match_basis": "legacy_exact_symbol_qty_chronology",
                "symbol": buy_symbol,
                "filled_qty": str(buy_qty),
                "buy": _row_ref(buy),
                "sell": _row_ref(candidate),
            }
        )

    return matched_buy_tokens, legacy_matches, source_issues


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


def _verify_broker_snapshot(
    *,
    rows: list[dict[str, Any]] | None,
    container: Any = None,
    fetched_at: Any,
    checked_at: datetime,
    max_age_minutes: int,
) -> dict[str, Any]:
    """Classify whether a caller-supplied broker snapshot counts as evidence.

    ROB-1130: an empty list is not evidence of an empty account. The caller must
    also state when the snapshot was fetched, otherwise "positions=0 because we
    looked" is indistinguishable from "positions=0 because we did not look" and
    the gate silently passes while the account holds positions.

    ``container`` is the value as the caller passed it, before list
    normalization. A mapping or string is not a broker row sequence: handing the
    whole MCP response envelope (or an empty ``{}``) to the gate would otherwise
    normalize to ``[]`` and read as a genuinely flat account.
    """
    state: dict[str, Any] = {
        "provided": rows is not None,
        "count": len(rows) if rows is not None else None,
        "fetched_at": None,
        "verified": False,
        "reason": None,
    }
    if rows is None:
        state["reason"] = "snapshot_missing"
        return state
    if isinstance(container, (Mapping, str, bytes)):
        # e.g. positions={"success": True, "positions": [...]} or positions={}.
        state["count"] = None
        state["reason"] = "snapshot_container_not_a_row_list"
        return state
    if fetched_at is None or (isinstance(fetched_at, str) and not fetched_at.strip()):
        state["reason"] = "snapshot_not_attested"
        return state
    parsed = _as_aware_utc(_parse_datetime(fetched_at))
    if parsed is None:
        state["reason"] = "snapshot_attestation_unparseable"
        return state
    state["fetched_at"] = parsed.isoformat()
    if parsed > checked_at + _SNAPSHOT_FUTURE_TOLERANCE:
        state["reason"] = "snapshot_attestation_in_future"
        return state
    if parsed < checked_at - timedelta(minutes=max_age_minutes):
        state["reason"] = "snapshot_stale"
        return state
    state["verified"] = True
    return state


def _verify_snapshot_account(
    *,
    expected_account_mode: str | None,
    snapshot_account_mode: str | None,
) -> dict[str, Any]:
    """Classify whether the snapshot is attested to the account being gated.

    ROB-1130: the required pre-gate reads are positions, open orders **and the
    account identity** they were read from. Two Alpaca paper account modes
    (``alpaca_paper`` and ``alpaca_paper_lab``) use different credentials and
    hold different inventory, so a fresh, correctly attested snapshot of the
    *other* account still reads as "this account is flat".
    """
    expected = _clean_lower(expected_account_mode)
    attested = _clean_lower(snapshot_account_mode)
    state: dict[str, Any] = {
        "expected": expected,
        "attested": attested,
        "verified": False,
        "reason": None,
    }
    if not expected:
        # No gated account declared: identity cannot be checked and is not
        # claimed. Only non-authorizing callers reach this branch.
        state["reason"] = "account_scope_not_declared"
        return state
    if not attested:
        state["reason"] = "snapshot_account_unattested"
        return state
    if attested != expected:
        state["reason"] = "snapshot_account_mismatch"
        return state
    state["verified"] = True
    return state


def build_paper_execution_preflight_report(
    *,
    ledger_rows: Iterable[Any] = (),
    open_orders: Iterable[dict[str, Any]] | None = None,
    positions: Iterable[dict[str, Any]] | None = None,
    open_orders_fetched_at: datetime | str | None = None,
    positions_fetched_at: datetime | str | None = None,
    snapshot_max_age_minutes: int = DEFAULT_SNAPSHOT_MAX_AGE_MINUTES,
    snapshot_evidence_required: bool = True,
    expected_account_mode: str | None = None,
    snapshot_account_mode: str | None = None,
    approval_packet: dict[str, Any] | None = None,
    expected_signal_symbol: str | None = None,
    expected_execution_symbol: str | None = None,
    now: datetime | None = None,
    stale_after_minutes: int = 30,
    legacy_cycle_blockers_as_warnings: bool = False,
) -> PaperExecutionPreflightReport:
    """Build a read-only Alpaca Paper execution preflight anomaly report.

    Args:
        ledger_rows: Recent or correlation-scoped ledger rows. ORM rows and
            dictionaries are both accepted for deterministic tests.
        open_orders: Read-only broker open-order snapshot already fetched by
            the caller. Non-terminal rows block a new cycle. ``None`` means the
            snapshot was never fetched, which is itself a blocker.
        positions: Read-only position snapshot already fetched by the caller.
            Any non-zero quantity blocks a new cycle. The snapshot is also the
            evidence used to tell a still-open buy leg apart from a buy whose
            sell leg was never recorded (ROB-1129). ``None`` means the snapshot
            was never fetched, which is itself a blocker.
        open_orders_fetched_at: When the open-order snapshot was read from the
            broker. Required for the snapshot to count as evidence (ROB-1130).
        positions_fetched_at: When the position snapshot was read from the
            broker. Required for the snapshot to count as evidence (ROB-1130).
        snapshot_max_age_minutes: Maximum snapshot age that still counts as a
            current view of the account.
        snapshot_evidence_required: When True (the default, and the only correct
            setting for any pre-order gate) a missing, unattested, stale, or
            unparseable broker snapshot is a blocker. Historical audit report
            builders that are not authorizing an order may set this to False;
            it does not downgrade any other blocker.
        expected_account_mode: The Alpaca paper account being gated. Order
            gates must declare it so the snapshot can be checked against the
            account it authorizes (ROB-1130). When omitted, snapshot account
            identity is reported as not declared and the positions snapshot is
            not treated as account evidence.
        snapshot_account_mode: The account the caller actually read the
            snapshot from, as echoed by ``alpaca_paper_list_positions`` /
            ``alpaca_paper_list_orders``. A different account's fresh snapshot
            is not evidence about this account.
        approval_packet: Optional preview/approval packet being considered for
            execution. Used for duplicate, stale, and symbol checks.
        expected_signal_symbol: Optional symbol from the signal artifact.
        expected_execution_symbol: Optional symbol expected at Alpaca Paper.
        now: Clock injection for deterministic tests.
        stale_after_minutes: Preview/approval max age before blocking.
        legacy_cycle_blockers_as_warnings: When True, downgrade the legacy
            single-cycle cleanup gates (residual positions and stale preview
            rows) to warnings. This is intended for Alpaca Paper execution-flow
            testing where operators deliberately exercise buy/sell/adjust/close
            paths against an already-used paper account. Open orders,
            duplicate client IDs, ledger/order/fill mismatches, unclosed sells,
            missing linked sells, unverified broker snapshots, and symbol
            mismatches still block.
    """
    if snapshot_max_age_minutes < 1:
        raise ValueError("snapshot_max_age_minutes must be >= 1")
    checked_at = _as_aware_utc(now) or datetime.now(UTC)
    unscoped_ledger = list(ledger_rows)
    orders = _snapshot_rows(open_orders)
    position_rows = _snapshot_rows(positions)
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

    # 0. Broker snapshot evidence (ROB-1130). This runs first because every
    # check below that concludes "nothing is there" depends on it.
    snapshot_state: dict[str, Any] = {
        "positions": _verify_broker_snapshot(
            rows=position_rows,
            container=positions,
            fetched_at=positions_fetched_at,
            checked_at=checked_at,
            max_age_minutes=snapshot_max_age_minutes,
        ),
        "open_orders": _verify_broker_snapshot(
            rows=orders,
            container=open_orders,
            fetched_at=open_orders_fetched_at,
            checked_at=checked_at,
            max_age_minutes=snapshot_max_age_minutes,
        ),
        "max_age_minutes": snapshot_max_age_minutes,
        "evidence_required": snapshot_evidence_required,
    }
    account_state = _verify_snapshot_account(
        expected_account_mode=expected_account_mode,
        snapshot_account_mode=snapshot_account_mode,
    )
    snapshot_state["account"] = account_state
    # An unverified account identity invalidates both snapshot kinds: a fresh,
    # well-formed snapshot of a different Alpaca paper account still reads as
    # "this account is flat". Marking the kinds unverified keeps one blocker
    # path and stops the counts from reporting someone else's zero.
    if not account_state["verified"] and expected_account_mode is not None:
        for kind in _SNAPSHOT_KINDS:
            kind_state = snapshot_state[kind]
            if kind_state["verified"]:
                kind_state["verified"] = False
                kind_state["reason"] = account_state["reason"]
    unverified_kinds = [
        kind for kind in _SNAPSHOT_KINDS if not snapshot_state[kind]["verified"]
    ]
    if snapshot_evidence_required and unverified_kinds:
        add(
            "broker_snapshot_unverified",
            PaperExecutionAnomalySeverity.block,
            "Broker state snapshot is missing or unverified, so an empty "
            "account cannot be distinguished from an unqueried one",
            {
                "unverified": unverified_kinds,
                "reasons": {
                    kind: snapshot_state[kind]["reason"] for kind in unverified_kinds
                },
                "max_age_minutes": snapshot_max_age_minutes,
                "required_reads": [
                    "alpaca_paper_list_positions",
                    "alpaca_paper_list_orders(status=open)",
                    "account identity (account_mode echoed by those reads)",
                ],
                "remediation": (
                    "Fetch the broker snapshot immediately before the gate and "
                    "pass it together with the time it was fetched and the "
                    "account_mode it was read from."
                ),
                "account": account_state,
                "snapshot": {kind: snapshot_state[kind] for kind in _SNAPSHOT_KINDS},
            },
        )

    position_rows = position_rows if position_rows is not None else []
    malformed_positions = _position_snapshot_issues(position_rows)
    if malformed_positions:
        add(
            "broker_position_snapshot_malformed",
            PaperExecutionAnomalySeverity.block,
            "Broker position snapshot contains a row with missing or invalid "
            "symbol/quantity evidence",
            {
                "count": len(malformed_positions),
                "rows": malformed_positions[:10],
            },
        )

    positions_are_evidence = (
        snapshot_state["positions"]["verified"]
        if snapshot_evidence_required
        else bool(position_rows)
    ) and not malformed_positions
    orders = orders if orders is not None else []

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
    residual_positions: list[tuple[dict[str, Any], Decimal]] = []
    for position in position_rows:
        if not isinstance(position, dict):
            continue
        qty = _strict_position_qty(position)
        if qty is not None and qty != Decimal("0"):
            residual_positions.append((position, qty))
    if residual_positions:
        add(
            "residual_position_exists",
            PaperExecutionAnomalySeverity.warning
            if legacy_cycle_blockers_as_warnings
            else PaperExecutionAnomalySeverity.block,
            "Residual Alpaca Paper position exists before starting a new cycle",
            {
                "count": len(residual_positions),
                "positions": [
                    {
                        "symbol": p.get("symbol"),
                        "qty": str(qty),
                        "asset_class": p.get("asset_class"),
                    }
                    for p, qty in residual_positions
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

    # 4. Previous buy filled but no completed sell evidence exists.
    #
    # ROB-1129: reaching a filled/reconciled state is NOT by itself evidence
    # that a sell leg is missing. A buy that is still holding an open position
    # legitimately has no sell row yet, and reporting it as an anomaly turned
    # normal open positions into preflight blockers. The live position snapshot
    # is the only thing that separates the two cases:
    #
    #   completed sell evidence              -> closed leg, expected
    #   unmatched buy + enough held quantity -> open position, expected (info)
    #   unmatched buy + insufficient holding -> unresolved lifecycle (block)
    #
    # Trusting the snapshot is a separate concern; when no snapshot is available
    # the row stays a blocker rather than being assumed open (fail-closed).
    (
        matched_buy_tokens,
        legacy_buy_sell_matches,
        source_evidence_issues,
    ) = _match_filled_buys_to_sells(ledger)
    if source_evidence_issues:
        add(
            "sell_source_evidence_invalid",
            PaperExecutionAnomalySeverity.block,
            "Explicit sell source evidence is ambiguous, incomplete, or "
            "inconsistent with the source buy",
            {
                "count": len(source_evidence_issues),
                "rows": source_evidence_issues[:10],
            },
        )
    if legacy_buy_sell_matches:
        add(
            "legacy_buy_sell_match",
            PaperExecutionAnomalySeverity.info,
            "Historical buy and sell legs have exact fill evidence despite missing "
            "source-link provenance",
            {
                "count": len(legacy_buy_sell_matches),
                "rows": legacy_buy_sell_matches[:10],
            },
        )

    remaining_position_qty, invalid_position_symbols = _position_qty_by_symbol(
        position_rows
    )
    positions_verified = positions_are_evidence
    open_position_buys: list[Any] = []
    unresolved_buys: list[dict[str, Any]] = []
    buy_rows = [
        row
        for row in ledger
        if str(_get(row, "side") or "").lower() == "buy"
        and str(_get(row, "lifecycle_state") or "").lower()
        in _BUY_REQUIRES_LINKED_SELL_STATES
    ]
    buy_rows.sort(
        key=lambda row: (
            _ledger_row_created_at(row) or datetime.max.replace(tzinfo=UTC),
            str(_get(row, "client_order_id") or ""),
        )
    )
    for row in buy_rows:
        side = str(_get(row, "side") or "").lower()
        state = str(_get(row, "lifecycle_state") or "").lower()
        if side != "buy" or state not in _BUY_REQUIRES_LINKED_SELL_STATES:
            continue
        if id(row) in matched_buy_tokens:
            continue
        if state in _BUY_COMPLETED_ROUNDTRIP_STATES:
            unresolved_buys.append(
                {"reason": "completed_state_without_sell_leg", **_row_ref(row)}
            )
            continue
        if not positions_verified:
            unresolved_buys.append(
                {"reason": "position_snapshot_unverified", **_row_ref(row)}
            )
            continue
        symbol = _ledger_row_broker_symbol(row)
        filled_qty = _strict_decimal(_get(row, "filled_qty"))
        available_qty = remaining_position_qty.get(symbol, Decimal("0"))
        if filled_qty is None or filled_qty <= Decimal("0"):
            unresolved_buys.append(
                {"reason": "holding_state_filled_qty_invalid", **_row_ref(row)}
            )
            continue
        if (
            symbol
            and symbol not in invalid_position_symbols
            and available_qty >= filled_qty
        ):
            open_position_buys.append(row)
            remaining_position_qty[symbol] = available_qty - filled_qty
        else:
            reason = (
                "holding_state_without_open_position"
                if available_qty == Decimal("0")
                else "holding_state_without_sufficient_open_position"
            )
            unresolved_buys.append(
                {
                    "reason": reason,
                    "broker_position_qty_remaining": str(available_qty),
                    **_row_ref(row),
                }
            )

    if open_position_buys:
        add(
            "open_position_without_sell_leg",
            PaperExecutionAnomalySeverity.info,
            "Filled buy has no sell leg because the position is still open",
            {
                "count": len(open_position_buys),
                "rows": [_row_ref(r) for r in open_position_buys[:10]],
            },
        )
    if unresolved_buys:
        add(
            "previous_buy_filled_sell_missing",
            PaperExecutionAnomalySeverity.block,
            "A previous filled buy has no linked sell ledger row",
            {
                "count": len(unresolved_buys),
                "rows": unresolved_buys[:10],
            },
        )

    # 5. Sell filled but final position not closed.
    #
    # ``sell_claim_baseline`` is captured before submit and proves only that the
    # quantity was available to sell. It is not a post-fill position snapshot.
    # Prefer a stored post-fill zero snapshot; otherwise require a verified,
    # current broker snapshot to prove the symbol is flat. A current non-zero
    # position remains blocking because a later re-entry cannot be distinguished
    # without stronger lifecycle provenance.
    sells_closed_by_current_snapshot: list[dict[str, Any]] = []
    sells_not_closed: list[dict[str, Any]] = []
    current_position_qty, invalid_position_symbols = _position_qty_by_symbol(
        position_rows
    )
    for row in ledger:
        if not _is_filled_sell_evidence(row):
            continue
        snapshot = _get(row, "position_snapshot") or {}
        snapshot_kind = (
            str(snapshot.get("snapshot_kind") or "")
            if isinstance(snapshot, dict)
            else ""
        )
        if (
            isinstance(snapshot, dict)
            and snapshot_kind != "sell_claim_baseline"
            and _strict_position_qty(snapshot) == Decimal("0")
        ):
            continue
        if not positions_verified:
            sells_not_closed.append(
                {"reason": "position_snapshot_unverified", **_row_ref(row)}
            )
            continue
        symbol = _ledger_row_broker_symbol(row)
        if not symbol:
            sells_not_closed.append(
                {"reason": "execution_symbol_missing", **_row_ref(row)}
            )
            continue
        if symbol in invalid_position_symbols:
            sells_not_closed.append(
                {"reason": "broker_position_qty_invalid", **_row_ref(row)}
            )
            continue
        broker_qty = current_position_qty.get(symbol, Decimal("0"))
        if broker_qty == Decimal("0"):
            sells_closed_by_current_snapshot.append(
                {
                    "reason": "verified_current_broker_position_flat",
                    "stored_snapshot_kind": snapshot_kind or None,
                    **_row_ref(row),
                }
            )
        else:
            sells_not_closed.append(
                {
                    "reason": "verified_current_broker_position_nonzero",
                    "broker_position_qty": str(broker_qty),
                    "stored_snapshot_kind": snapshot_kind or None,
                    **_row_ref(row),
                }
            )
    if sells_closed_by_current_snapshot:
        add(
            "sell_closed_by_current_position_snapshot",
            PaperExecutionAnomalySeverity.info,
            "Filled sell is closed by a verified current flat broker position",
            {
                "count": len(sells_closed_by_current_snapshot),
                "rows": sells_closed_by_current_snapshot[:10],
            },
        )
    if sells_not_closed:
        add(
            "sell_filled_position_not_closed",
            PaperExecutionAnomalySeverity.block,
            "A filled sell lacks verified evidence that its final position is closed",
            {"rows": sells_not_closed[:10]},
        )

    # 6. Ledger/order/fill mismatches.
    mismatches = []
    for row in ledger:
        side = str(_get(row, "side") or "").lower()
        state = str(_get(row, "lifecycle_state") or "").lower()
        order_status = str(_get(row, "order_status") or "").lower()
        filled_qty = _strict_decimal(_get(row, "filled_qty"))
        if state == "filled" and (filled_qty is None or filled_qty <= Decimal("0")):
            mismatches.append(
                {"reason": "filled_state_without_filled_qty", **_row_ref(row)}
            )
        if side == "sell":
            if state == "filled" and order_status != "filled":
                mismatches.append(
                    {
                        "reason": "filled_lifecycle_without_terminal_order_status",
                        **_row_ref(row),
                    }
                )
            elif state in _CANONICAL_FILLED_LEDGER_STATES and order_status != "filled":
                mismatches.append(
                    {
                        "reason": "completed_sell_without_terminal_order_status",
                        **_row_ref(row),
                    }
                )
            elif state in _OPEN_LEDGER_STATES:
                mismatches.append(
                    {"reason": "sell_lifecycle_not_terminal", **_row_ref(row)}
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
    terminal_siblings_by_correlation: dict[str, list[Any]] = {}
    for row in unscoped_ledger:
        correlation_id = _scope_value(_get(row, "lifecycle_correlation_id"))
        lifecycle_state = str(_get(row, "lifecycle_state") or "").lower()
        if correlation_id and lifecycle_state in _TERMINAL_PREVIEW_SIBLING_STATES:
            terminal_siblings_by_correlation.setdefault(correlation_id, []).append(row)

    stale_preview_rows = []
    for row in ledger:
        state = str(_get(row, "lifecycle_state") or "").lower()
        if state not in {"previewed", "validation_failed"}:
            continue
        preview_time = _latest_preview_time(row)
        if preview_time is not None and preview_time < stale_cutoff:
            stale_preview_rows.append(row)

    spent_preview_rows: list[tuple[Any, Any]] = []
    blocking_stale_rows = []
    for row in stale_preview_rows:
        correlation_id = _scope_value(_get(row, "lifecycle_correlation_id"))
        terminal_sibling = next(
            (
                sibling
                for sibling in terminal_siblings_by_correlation.get(correlation_id, [])
                if sibling is not row
            ),
            None,
        )
        if terminal_sibling is None:
            blocking_stale_rows.append(row)
        else:
            spent_preview_rows.append((row, terminal_sibling))

    if spent_preview_rows:
        add(
            "spent_preview_without_cleanup",
            PaperExecutionAnomalySeverity.warning,
            "A stale preview has a terminal lifecycle sibling but was not cleaned up",
            {
                "stale_after_minutes": stale_after_minutes,
                "cutoff": stale_cutoff.isoformat(),
                "count": len(spent_preview_rows),
                "rows": [
                    {
                        **_cleanup_required_row_ref(row),
                        "terminal_sibling_client_order_id": _get(
                            terminal_sibling, "client_order_id"
                        ),
                        "terminal_sibling_lifecycle_state": _get(
                            terminal_sibling, "lifecycle_state"
                        ),
                    }
                    for row, terminal_sibling in spent_preview_rows[:10]
                ],
            },
        )

    stale_rows = list(blocking_stale_rows)
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
            PaperExecutionAnomalySeverity.warning
            if legacy_cycle_blockers_as_warnings
            else PaperExecutionAnomalySeverity.block,
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
    counts: dict[str, int] = {
        "ledger_rows": len(ledger),
        "unscoped_ledger_rows": len(unscoped_ledger),
        "block": sum(
            a.severity == PaperExecutionAnomalySeverity.block for a in anomalies
        ),
        "warning": sum(
            a.severity == PaperExecutionAnomalySeverity.warning for a in anomalies
        ),
        "info": sum(
            a.severity == PaperExecutionAnomalySeverity.info for a in anomalies
        ),
    }
    # Only report a snapshot count that actually describes the account. Emitting
    # ``positions: 0`` for a snapshot we could not verify is the exact false
    # assurance ROB-1130 was filed for.
    for kind in _SNAPSHOT_KINDS:
        kind_state = snapshot_state[kind]
        if kind_state["count"] is None:
            continue
        if kind_state["verified"] or not snapshot_evidence_required:
            counts[kind] = kind_state["count"]
    return PaperExecutionPreflightReport(
        status="blocked" if should_block else "pass",
        should_block=should_block,
        checked_at=checked_at,
        stale_after_minutes=stale_after_minutes,
        anomalies=tuple(anomalies),
        counts=counts,
        broker_snapshot=snapshot_state,
    )


__all__ = [
    "DEFAULT_SNAPSHOT_MAX_AGE_MINUTES",
    "STALE_PREVIEW_CLEANUP_ACTION",
    "STALE_PREVIEW_CLEANUP_REQUIRED_STATE",
    "PaperExecutionAnomaly",
    "PaperExecutionAnomalySeverity",
    "PaperExecutionPreflightReport",
    "build_paper_execution_preflight_report",
]
