"""Evidence-first reconcile for manually submitted Alpaca paper orders (ROB-953)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.alpaca_paper_ledger_service import (
    LIFECYCLE_ANOMALY,
    LIFECYCLE_CANCELED,
    LIFECYCLE_FILLED,
    LIFECYCLE_SUBMITTED,
    RECONCILE_TERMINAL_LIFECYCLE_STATES,
    RECORD_KIND_EXECUTION,
    StatusWriteResult,
    derive_lifecycle_state,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
    classify_fill_evidence,
)

# Alpaca's FILL activity feed is account-wide and page-capped, so one read can
# omit fills belonging to an older order. Walk pages forward, bounded.
_FILL_PAGE_LIMIT = 100
_MAX_FILL_PAGES = 20

# ---------------------------------------------------------------------------
# ROB-953 — the single (evidence -> persisted state -> reported action) mapping
# ---------------------------------------------------------------------------
# Both the ``action`` returned to the caller and the ``lifecycle_state`` written
# to the ledger are derived from ONE resolution below. Earlier revisions of this
# PR computed them in two independent if/else ladders, which reported
# ``booked_filled`` for a rejected order that actually persisted as ``anomaly``
# (and ``booked_anomaly`` for rows that persisted as ``submitted``).
#
# The codomain of ``derive_lifecycle_state`` is exactly these four states.
_LIFECYCLE_ACTION_LABELS: dict[str, str] = {
    LIFECYCLE_FILLED: "filled",
    # A booking transition that lands on ``submitted`` is by construction a
    # partial fill: an open status carrying qty>0 derives ``anomaly`` instead.
    LIFECYCLE_SUBMITTED: "partial",
    LIFECYCLE_ANOMALY: "anomaly",
    LIFECYCLE_CANCELED: "canceled",
}


def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
    """Stringify Decimals so the evidence can land in the JSONB raw_responses."""
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in payload.items()
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ReconcileTransition:
    """Binds the persisted lifecycle state to the reported action, one source.

    ``broker_status`` is exactly the ``status`` handed to ``record_status``, and
    ``lifecycle_state`` is what ``record_status`` will derive from it — same
    function, same inputs — so the reported action cannot drift from the row.
    """

    broker_status: str
    lifecycle_state: str

    @property
    def label(self) -> str:
        return _LIFECYCLE_ACTION_LABELS.get(self.lifecycle_state, self.lifecycle_state)

    def action(self, *, dry_run: bool) -> str:
        return f"would_book_{self.label}" if dry_run else f"booked_{self.label}"


def resolve_transition(
    *,
    verdict: FillVerdict,
    broker_status: Any,
    filled_qty: Decimal,
    has_cancel_evidence: bool = False,
) -> ReconcileTransition:
    """Resolve the ledger status payload and lifecycle state from one evidence set."""
    status = str(broker_status or "").strip().lower()
    if verdict is FillVerdict.PARTIAL:
        # The broker may report ``filled`` while the evidence only proves a
        # partial cumulative quantity. Never promote past the evidence.
        status = "partially_filled"
    return ReconcileTransition(
        broker_status=status,
        lifecycle_state=derive_lifecycle_state(
            status, filled_qty, has_cancel_evidence=has_cancel_evidence
        ),
    )


@dataclass(frozen=True)
class FillSetEvidence:
    """Whether the observed fills fully account for the broker's cumulative qty."""

    fills: list[Any]
    summed_qty: Decimal | None
    cumulative_qty: Decimal | None
    complete: bool
    reason: str | None

    @property
    def weighted_avg_price(self) -> Decimal | None:
        """Quantity-weighted average, computed only from a complete fill set."""
        if not self.complete:
            return None
        total_qty = Decimal("0")
        total_notional = Decimal("0")
        for fill in self.fills:
            qty = _decimal(getattr(fill, "qty", None))
            price = _decimal(getattr(fill, "price", None))
            if qty is None or price is None:
                return None
            total_qty += qty
            total_notional += qty * price
        if total_qty <= 0:
            return None
        return total_notional / total_qty


def summarize_fill_set(order: Any, fills: list[Any] | None) -> FillSetEvidence:
    """Check the fills for ``order`` against the broker's own cumulative quantity.

    A weighted average over a truncated fill set is simply a wrong price, so
    completeness is proven before any average is derived. The authority is the
    order's ``filled_qty``; when the order omits it, the highest ``cum_qty``
    the fills themselves claim is used instead.
    """
    relevant = [
        fill for fill in fills or [] if getattr(fill, "order_id", None) == order.id
    ]
    per_fill = [_decimal(getattr(fill, "qty", None)) for fill in relevant]
    summed: Decimal | None = (
        None
        if any(qty is None for qty in per_fill)
        else sum((qty for qty in per_fill if qty is not None), Decimal("0"))
    )
    cum_claims = [
        cum
        for cum in (_decimal(getattr(fill, "cum_qty", None)) for fill in relevant)
        if cum is not None
    ]
    cumulative = _decimal(getattr(order, "filled_qty", None))
    if cumulative is None:
        cumulative = max(cum_claims, default=None)

    if cumulative is None:
        return FillSetEvidence(relevant, summed, None, False, "cumulative_qty_unknown")
    if summed is None:
        return FillSetEvidence(
            relevant, None, cumulative, False, "unparseable_fill_quantity"
        )
    if summed != cumulative:
        return FillSetEvidence(
            relevant, summed, cumulative, False, "incomplete_fill_set"
        )
    return FillSetEvidence(relevant, summed, cumulative, True, None)


def normalize_alpaca_order_for_classify(
    order: Any, fills: list[Any] | None = None
) -> dict[str, Any]:
    """Adapt Alpaca's cumulative order/fill values to the shared classifier shape."""
    fill_set = summarize_fill_set(order, fills)
    filled_qty = _decimal(getattr(order, "filled_qty", None))
    if filled_qty is None:
        filled_qty = fill_set.cumulative_qty
    avg_price = _decimal(getattr(order, "filled_avg_price", None))
    if avg_price is None:
        avg_price = fill_set.weighted_avg_price
    return {
        "odno": order.id,
        "ord_qty": getattr(order, "qty", None),
        "tot_ccld_qty": filled_qty,
        "ccld_unpr": avg_price,
    }


class AlpacaPaperReconcileService:
    """Reconcile existing non-terminal ledger rows using read-only broker evidence."""

    def __init__(self, ledger: Any, broker: Any) -> None:
        self._ledger = ledger
        self._broker = broker

    async def reconcile(
        self,
        *,
        symbol: str | None = None,
        client_order_id: str | None = None,
        dry_run: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        if client_order_id is not None:
            row = await self._ledger.get_execution_by_client_order_id(client_order_id)
            rows = [row] if row is not None else []
        else:
            # Eligibility is filtered in SQL before ``limit`` so older open
            # executions stay reachable behind newer preview/terminal rows.
            rows = await self._ledger.list_reconcile_candidates(
                limit=limit, symbol=symbol
            )
        # Re-assert the predicates in Python: defence in depth, and the only
        # filter for the single-order lookup path.
        candidates = [
            row
            for row in rows
            if getattr(row, "record_kind", None) == RECORD_KIND_EXECUTION
            and getattr(row, "lifecycle_state", None)
            not in RECONCILE_TERMINAL_LIFECYCLE_STATES
            and (symbol is None or getattr(row, "execution_symbol", None) == symbol)
            and (
                client_order_id is None
                or getattr(row, "client_order_id", None) == client_order_id
            )
        ]
        outcomes = [
            await self._reconcile_one(row, dry_run=dry_run) for row in candidates
        ]
        return {
            "success": True,
            "dry_run": dry_run,
            "reconciled": outcomes,
            "count": len(outcomes),
        }

    async def _load_order_fills(self, order: Any) -> list[Any]:
        """Page every FILL activity, then keep the ones belonging to ``order``."""
        collected: dict[str, Any] = {}
        page_token: str | None = None
        for _ in range(_MAX_FILL_PAGES):
            page = list(
                await self._broker.list_fills(
                    page_token=page_token,
                    page_size=_FILL_PAGE_LIMIT,
                    direction="desc",
                )
                or []
            )
            for fill in page:
                fill_id = getattr(fill, "id", None)
                collected.setdefault(
                    str(fill_id) if fill_id is not None else str(id(fill)), fill
                )
            if len(page) < _FILL_PAGE_LIMIT:
                break
            last_activity_id = getattr(page[-1], "id", None)
            next_page_token = (
                str(last_activity_id) if last_activity_id is not None else None
            )
            if next_page_token is None or next_page_token == page_token:
                raise RuntimeError("fill_pagination_token_missing")
            page_token = next_page_token
        else:
            # Never treat a bounded/truncated account activity feed as complete.
            raise RuntimeError("fill_pagination_limit_exceeded")
        return [
            fill
            for fill in collected.values()
            if getattr(fill, "order_id", None) == order.id
        ]

    async def _reconcile_one(self, row: Any, *, dry_run: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ledger_id": row.id,
            "client_order_id": row.client_order_id,
            "symbol": row.execution_symbol,
            "transition_depth": "none",
        }

        def manual_review(reason: str) -> dict[str, Any]:
            result.update(
                action="noop_requires_manual_review",
                requires_manual_review=True,
                reason=reason,
            )
            return result

        try:
            order = await self._broker.get_order_by_client_order_id(row.client_order_id)
        except Exception as exc:  # read failure is never execution evidence
            return manual_review(str(exc) or exc.__class__.__name__)
        if order is None:
            return manual_review("broker_order_not_found")

        broker_status = str(getattr(order, "status", "") or "").strip().lower()
        cumulative = _decimal(getattr(order, "filled_qty", None))
        broker_avg = _decimal(getattr(order, "filled_avg_price", None))

        # A terminal filled status must be backed by the complete activity set,
        # even when the order payload already carries qty/average fields.
        needs_fills = (
            broker_status == "filled"
            or cumulative is None
            or (cumulative > 0 and broker_avg is None)
        )
        fills: list[Any] = []
        if needs_fills:
            try:
                fills = await self._load_order_fills(order)
            except Exception as exc:
                return manual_review(str(exc) or exc.__class__.__name__)
            fill_set = summarize_fill_set(order, fills)
            if not fill_set.complete:
                # A truncated / unreadable fill set yields a wrong average, so it
                # is escalated rather than partially booked.
                result["fill_set_complete"] = False
                return manual_review(fill_set.reason or "incomplete_fill_set")
            result["fill_set_complete"] = True

        evidence: FillEvidence = classify_fill_evidence(
            order_no=order.id, rows=[normalize_alpaca_order_for_classify(order, fills)]
        )
        result["verdict"] = evidence.verdict.value

        if evidence.verdict is FillVerdict.NONE:
            return manual_review(evidence.reason_code)
        if evidence.verdict is FillVerdict.PENDING:
            if broker_status == "filled":
                # The broker calls the order filled but produced no quantity to
                # book. That contradiction is escalated, never a silent no-op.
                return manual_review("filled_status_without_fill_evidence")
            result.update(action="noop_pending")
            return result

        broker_qty = evidence.filled_qty or Decimal("0")
        already = _decimal(getattr(row, "filled_qty", None)) or Decimal("0")
        result.update(
            filled_qty=str(broker_qty),
            avg_price=str(evidence.avg_price),
            delta_qty=str(broker_qty - already),
        )
        if broker_qty <= already:
            result.update(
                action="noop_already_booked",
                transition_depth=getattr(row, "lifecycle_state", "none"),
            )
            return result

        transition = resolve_transition(
            verdict=evidence.verdict,
            broker_status=broker_status,
            filled_qty=broker_qty,
            has_cancel_evidence=(
                getattr(row, "cancel_status", None) is not None
                or getattr(row, "canceled_at", None) is not None
            ),
        )
        result["transition_depth"] = transition.label
        if dry_run:
            result["lifecycle_state"] = transition.lifecycle_state
            result["action"] = transition.action(dry_run=True)
            return result

        status_write = await self._ledger.record_status(
            row.client_order_id,
            {
                "status": transition.broker_status,
                "filled_qty": str(broker_qty),
                "filled_avg_price": str(evidence.avg_price),
                "id": order.id,
            },
            # raw_response is persisted to a JSONB column: Decimal is not JSON
            # serializable, so the evidence is stringified here.
            raw_response={
                "reconcile_order": _json_safe(
                    normalize_alpaca_order_for_classify(order, fills)
                )
            },
            return_write_result=True,
        )
        if isinstance(status_write, StatusWriteResult):
            write_applied = status_write.applied
            confirmed = status_write.row
        else:
            # Lightweight test doubles retain the historical row return contract.
            write_applied = True
            confirmed = status_write

        persisted = getattr(confirmed, "lifecycle_state", None)
        persisted_qty = _decimal(getattr(confirmed, "filled_qty", None))
        persisted_avg = _decimal(getattr(confirmed, "filled_avg_price", None))
        result["lifecycle_state"] = persisted
        result["persisted_lifecycle_state"] = persisted
        if persisted_qty is not None:
            result["filled_qty"] = str(persisted_qty)
        if persisted_avg is not None:
            result["avg_price"] = str(persisted_avg)

        write_confirmed = (
            write_applied
            and persisted == transition.lifecycle_state
            and persisted_qty == broker_qty
        )
        if not write_confirmed:
            # The SQL guard rejected the attempt, or a later concurrent write is
            # already visible. Report the fresh row, never the precomputed intent.
            result.update(
                action="noop_stale_write_rejected",
                transition_depth=(
                    _LIFECYCLE_ACTION_LABELS.get(persisted, persisted or "none")
                ),
                delta_qty="0",
                requires_manual_review=True,
                stale_write_rejected=True,
            )
            return result

        persisted_label = _LIFECYCLE_ACTION_LABELS.get(
            persisted, persisted or transition.label
        )
        result["transition_depth"] = persisted_label
        result["action"] = f"booked_{persisted_label}"
        return result


__all__ = [
    "AlpacaPaperReconcileService",
    "FillSetEvidence",
    "ReconcileTransition",
    "normalize_alpaca_order_for_classify",
    "resolve_transition",
    "summarize_fill_set",
]
