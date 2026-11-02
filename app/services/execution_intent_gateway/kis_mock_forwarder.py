"""ROB-1440 — forward child requests to the existing KIS mock order functions.

``is_mock=True`` is fixed here and no caller can change it. Every guard below the
gateway still applies: attribution, coordination route, follow-up capability,
ledger writes. If one of them refuses, the gateway records an unknown outcome and
does not send again.

ROB-1461: this module is the lifecycle owner for gateway-origin follow-ups. It
mints the operation-scoped capability that amend and cancel need, with the
gateway journal row as the durable claim. The scope of that decision is the
amendment in ``docs/contracts/rob-1263-kis-coordination-adapter.md`` §7: only a
kis_mock order this gateway sent can mint. An order from any other origin keeps
the §7 closure and gets zero broker calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

from app.schemas.execution_intent import AmendChild, CancelChild, SubmitChild
from app.services.execution_intent_gateway.decisions import (
    MOCK_ACCOUNTS,
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    _root_of,
)
from app.services.execution_intent_gateway.gateway import ForwardResult
from app.services.execution_intent_gateway.store import IntentJournal

if TYPE_CHECKING:
    from app.services.kis_mock_runner.singleton import (
        KISMockOperation,
        KISMockWriterLease,
    )

_MARKET = "kr"
_INSTRUMENT_TYPE = "equity_kr"
_FOLLOWUP_ACTIONS = frozenset({"submit", "amend"})
#: The ledger states of an order that may still rest at the broker. ``fill`` is
#: included because a partial fill leaves a remainder; the remainder check below
#: refuses a fully filled order.
OPEN_LEDGER_STATES = frozenset({"submitted", "accepted", "pending", "fill"})

#: Marks a ledger row as sent by this gateway. An order from another origin never
#: carries it, because only :meth:`KisMockForwarder.submit` writes it.
ORDER_CORRELATION_PREFIX: Final = "intent-gateway:"
CLAIM_SCOPE_PREFIX: Final = "execution-intent:"


def order_correlation_id(root_idempotency_key: str) -> str:
    return f"{ORDER_CORRELATION_PREFIX}{root_idempotency_key}"


def claim_scope(intent_id: UUID) -> str:
    return f"{CLAIM_SCOPE_PREFIX}{intent_id}"


class FollowupRefusal(StrEnum):
    """Why no capability was minted. Every one of these means zero broker calls."""

    ACCOUNT_NOT_MOCK = "followup_account_not_mock"
    INTENT_NOT_IN_JOURNAL = "followup_intent_not_in_journal"
    TARGET_NOT_IN_JOURNAL = "followup_target_not_in_journal"
    ORDER_ID_MISMATCH = "followup_order_id_mismatch"
    ORDER_NOT_IN_LEDGER = "followup_order_not_in_ledger"
    NOT_GATEWAY_ORIGIN = "followup_not_gateway_origin"
    LEDGER_TERMS_MISMATCH = "followup_ledger_terms_mismatch"
    ORDER_NOT_OPEN = "followup_order_not_open"
    REMAINDER_UNKNOWN = "followup_remainder_unknown"


class FollowupJournal(Protocol):
    async def read_intent_journal(self, intent_id: UUID) -> IntentJournal | None: ...


@dataclass(frozen=True, slots=True)
class _JournalClaim:
    """The gateway journal row, in the shape the capability issuer reads."""

    row_id: int
    idempotency_key: str
    side: str


def _text_or_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _is_claim_row(row: ChildSnapshot, broker_order_id: str) -> bool:
    """A sent order of this intent whose journaled order id is exactly the target."""
    return (
        row.action in _FOLLOWUP_ACTIONS
        and row.state is not ChildState.REFUSED
        and row.broker_order_id == broker_order_id
    )


def _known_remainder(ledger_order: Mapping[str, Any]) -> Decimal | None:
    raw = ledger_order.get("remaining_quantity")
    if isinstance(raw, bool) or not isinstance(raw, int | float | Decimal):
        return None
    try:
        remainder = Decimal(str(raw))
    except InvalidOperation:
        return None
    return remainder if remainder.is_finite() and remainder > 0 else None


def followup_refusal(
    intent: IntentSnapshot,
    *,
    journal: IntentJournal | None,
    target_key: str,
    broker_order_id: str,
    ledger_order: Mapping[str, Any] | None,
) -> FollowupRefusal | None:
    """Whether a follow-up on this order may mint. ``None`` allows it.

    Each check is one of the four bindings the §7 amendment names (target key,
    broker order id, intent, ledger correlation) or a fact the capability needs.
    The ledger correlation is what keeps every other origin closed: a coordinated
    or J2B-origin row was not written by this gateway's submit.
    """
    if intent.account not in MOCK_ACCOUNTS:
        return FollowupRefusal.ACCOUNT_NOT_MOCK
    if journal is None or journal.intent.intent_id != intent.intent_id:
        return FollowupRefusal.INTENT_NOT_IN_JOURNAL
    by_key = {child.idempotency_key: child for child in journal.children}
    target = by_key.get(target_key)
    if target is None:
        return FollowupRefusal.TARGET_NOT_IN_JOURNAL
    if not _is_claim_row(target, broker_order_id):
        return FollowupRefusal.ORDER_ID_MISMATCH
    if ledger_order is None:
        return FollowupRefusal.ORDER_NOT_IN_LEDGER
    root = _root_of(target_key, by_key)
    if ledger_order.get("correlation_id") != order_correlation_id(root):
        return FollowupRefusal.NOT_GATEWAY_ORIGIN
    if (
        ledger_order.get("symbol") != intent.symbol
        or ledger_order.get("side") != intent.side
        or ledger_order.get("instrument_type") != _INSTRUMENT_TYPE
    ):
        return FollowupRefusal.LEDGER_TERMS_MISMATCH
    if ledger_order.get("lifecycle_state") not in OPEN_LEDGER_STATES:
        return FollowupRefusal.ORDER_NOT_OPEN
    if _known_remainder(ledger_order) is None:
        return FollowupRefusal.REMAINDER_UNKNOWN
    return None


class KisMockForwarder:
    def __init__(
        self,
        *,
        journal: FollowupJournal,
        lease_factory: Callable[[], KISMockWriterLease] | None = None,
    ) -> None:
        self._journal = journal
        self._lease_factory = lease_factory

    async def submit(
        self, intent: IntentSnapshot, request: SubmitChild
    ) -> ForwardResult:
        from app.mcp_server.tooling.order_execution import _place_order_impl

        result = await _place_order_impl(
            symbol=intent.symbol,
            side=intent.side,
            market=_MARKET,
            order_type="limit",
            quantity=float(request.quantity),
            price=float(request.limit_price_krw),
            dry_run=False,
            reason=f"intent_gateway:{intent.intent_id}",
            strategy=intent.strategy,
            correlation_id=order_correlation_id(request.idempotency_key),
            is_mock=True,
        )
        return ForwardResult(
            accepted=result.get("success") is True,
            broker_order_id=_text_or_none(result.get("order_no")),
            result=result,
        )

    async def amend(
        self, intent: IntentSnapshot, request: AmendChild, *, broker_order_id: str
    ) -> ForwardResult:
        from app.mcp_server.tooling.orders_modify_cancel import modify_order_impl
        from app.services.kis_mock_runner.singleton import KISMockOperation

        async def send() -> ForwardResult:
            result = await modify_order_impl(
                order_id=broker_order_id,
                symbol=intent.symbol,
                market=_MARKET,
                new_price=float(request.limit_price_krw),
                new_quantity=float(request.quantity),
                dry_run=False,
                is_mock=True,
            )
            return ForwardResult(
                accepted=result.get("success") is True,
                broker_order_id=_text_or_none(result.get("new_order_id")),
                result=result,
            )

        return await self._with_capability(
            intent,
            operation=KISMockOperation.FOLLOWUP_MODIFY,
            target_key=request.target_idempotency_key,
            broker_order_id=broker_order_id,
            send=send,
        )

    async def cancel(
        self, intent: IntentSnapshot, request: CancelChild, *, broker_order_id: str
    ) -> ForwardResult:
        from app.mcp_server.tooling.orders_modify_cancel import cancel_order_impl
        from app.services.kis_mock_runner.singleton import KISMockOperation

        async def send() -> ForwardResult:
            result = await cancel_order_impl(
                order_id=broker_order_id,
                symbol=intent.symbol,
                market=_MARKET,
                is_mock=True,
            )
            # A soft cancel reports success while the order may still be live, so
            # only a confirmed broker cancel counts as accepted.
            return ForwardResult(
                accepted=result.get("success") is True
                and result.get("broker_cancel_confirmed") is True,
                broker_order_id=broker_order_id,
                result=result,
            )

        return await self._with_capability(
            intent,
            operation=KISMockOperation.FOLLOWUP_CANCEL,
            target_key=request.target_idempotency_key,
            broker_order_id=broker_order_id,
            send=send,
        )

    async def _with_capability(
        self,
        intent: IntentSnapshot,
        *,
        operation: KISMockOperation,
        target_key: str,
        broker_order_id: str,
        send: Callable[[], Awaitable[ForwardResult]],
    ) -> ForwardResult:
        """Mint a receipt for exactly this order and send while it is alive."""
        from app.mcp_server.tooling.kis_mock_ledger import (
            resolve_mock_order_for_cancel,
        )
        from app.services.kis_mock_runner.singleton import (
            issue_kis_mock_followup_capability,
        )

        journal = await self._journal.read_intent_journal(intent.intent_id)
        ledger_order = await resolve_mock_order_for_cancel(broker_order_id)
        refusal = followup_refusal(
            intent,
            journal=journal,
            target_key=target_key,
            broker_order_id=broker_order_id,
            ledger_order=ledger_order,
        )
        if refusal is not None:
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"success": False, "reason_code": refusal.value},
            )
        remainder = _known_remainder(ledger_order or {})
        if remainder is None:  # pragma: no cover - followup_refusal checked this
            raise RuntimeError("follow-up allowed with no known remainder")

        scope = claim_scope(intent.intent_id)

        async def reservations(account_scope: str) -> list[_JournalClaim]:
            # Read again inside the short lease: the claim must still hold.
            if account_scope != scope:
                return []
            current = await self._journal.read_intent_journal(intent.intent_id)
            if current is None:
                return []
            return [
                _JournalClaim(child.child_id, child.idempotency_key, intent.side)
                for child in current.children
                if child.idempotency_key == target_key
                and _is_claim_row(child, broker_order_id)
            ]

        lease = (
            {}
            if self._lease_factory is None
            else {"lease_factory": self._lease_factory}
        )
        async with issue_kis_mock_followup_capability(
            operation=operation,
            claim_account_scope=scope,
            claim_idempotency_key=target_key,
            attributed_broker_order_id=broker_order_id,
            known_remainder=remainder,
            reservations=reservations,
            **lease,
        ):
            return await send()
