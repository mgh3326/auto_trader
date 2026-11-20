"""Order-proposal side of the mandate send boundary (hk 3795 T4c, §4.3 (나), §4.6).

``app/services/mandate`` decides and writes the fence; this module is the only place
that connects it to the proposal send path. Three call sites use it:

* the approval callback, before the nonce is consumed (``pre_nonce_refusal``) — a fast
  refusal that leaves the nonce unspent;
* ``revalidate_and_submit`` — the fence transaction before the broker call, the
  composite pre-send hook at the broker's HTTP edge, and the closing of the fence
  after the outcome (release only for a proven-unsent attempt or an explicit
  rejection);
* the loss-sale guard (I14) at the same two places.

It only ever adds refusals. A proposal without ``mandate_plan_id`` never reaches the
mandate code: every function returns "no plan, nothing to do" first.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.mandate import fence
from app.services.mandate import vocabulary as v
from app.services.mandate.account_ref import derive_account_ref

logger = logging.getLogger(__name__)

SUPPORTED_ACTION = "place"
SUPPORTED_SIDE = "buy"


def is_plan_bound(group: object) -> bool:
    return getattr(group, "mandate_plan_id", None) is not None


def actual_account_identifier(account_mode: str) -> str | None:
    """The account the broker adapter will use, taken from the same settings it reads."""
    if account_mode == "kiwoom_mock":
        value = getattr(settings, "kiwoom_mock_account_no", None)
    elif account_mode == "kis_live":
        value = getattr(settings, "kis_account_no", None)
    elif account_mode == "toss_live":
        value = getattr(settings, "toss_api_account_seq", None)
    else:
        return None
    return None if value is None else str(value)


async def resolve_actual_account_ref(group: object) -> str | None:
    """The reference of the account this send will use; ``None`` = cannot compute (block)."""
    identifier = actual_account_identifier(str(getattr(group, "account_mode", "")))
    if identifier is None:
        return None
    return derive_account_ref(
        str(group.account_mode),  # type: ignore[attr-defined]
        identifier,
        secret=settings.SECRET_KEY,
    )


AccountRefResolver = Callable[[object], Awaitable[str | None]]


def fence_notional(rung: Any, preview: dict[str, Any] | None = None) -> Decimal | None:
    """max(limit, current price) x quantity; ``None`` when it cannot be a valid amount."""
    try:
        quantity = Decimal(str(rung.quantity))
        limit = Decimal(str(rung.limit_price))
    except (InvalidOperation, TypeError):
        return None
    price = limit
    if preview is not None:
        try:
            current = Decimal(str(preview.get("current_price")))
        except (InvalidOperation, TypeError):
            current = None
        if current is not None and current.is_finite() and current > price:
            price = current
    notional = price * quantity
    if not notional.is_finite() or notional <= 0:
        return None
    return notional


@dataclass
class MandateSendPort:
    """The three operations the send path needs, injectable for tests."""

    commit_fence: Callable[[fence.FenceRequest], Awaitable[fence.FenceOutcome]] = (
        fence.commit_send_fence
    )
    release_fence: Callable[..., Awaitable[fence.ReleaseOutcome]] = (
        fence.release_send_fence
    )
    boundary_refusal: Callable[..., Awaitable[str | None]] = fence.send_boundary_refusal
    account_ref_resolver: AccountRefResolver = resolve_actual_account_ref


DEFAULT_PORT = MandateSendPort()


def unsupported_shape_refusal(group: Any, rungs: list[Any]) -> str | None:
    """A plan-bound proposal is one resting buy limit rung (§4.1): anything else is refused."""
    if (group.action or "place") != SUPPORTED_ACTION:
        return v.MandateRefusal.UNSUPPORTED.value
    pending = [r for r in rungs if r.state == "pending_approval"]
    if len(pending) != 1 or pending[0].side != SUPPORTED_SIDE:
        return v.MandateRefusal.BINDING_MISMATCH.value
    return None


async def pre_nonce_refusal(
    group: Any,
    rungs: list[Any],
    *,
    now: datetime,
    port: MandateSendPort = DEFAULT_PORT,
) -> str | None:
    """A refusal before the nonce is spent, or ``None``. Advice only (§4.7).

    The fence transaction decides alone later; this just keeps a send that is going to
    be refused from using up the approval.
    """
    if not is_plan_bound(group):
        return None
    shape = unsupported_shape_refusal(group, rungs)
    if shape is not None:
        return shape
    rung = next(r for r in rungs if r.state == "pending_approval")
    notional = fence_notional(rung)
    if notional is None:
        return v.MandateRefusal.UNREADABLE.value
    try:
        account_ref = await port.account_ref_resolver(group)
    except Exception:  # noqa: BLE001 - an account that cannot be resolved blocks
        account_ref = None
    return await port.boundary_refusal(
        _request(group, notional, account_ref, now, "human")
    )


def _request(
    group: Any,
    notional: Decimal,
    account_ref: str | None,
    now: datetime,
    mode: str,
) -> fence.FenceRequest:
    return fence.FenceRequest(
        plan_id=int(group.mandate_plan_id),
        tranche_index=int(group.mandate_tranche_index),
        proposal_id=group.proposal_id,
        approval_mode=mode,
        notional=notional,
        actual_account_ref=account_ref,
        now=now,
    )


@dataclass
class SendFence:
    """One committed fence and the single POST it allows."""

    port: MandateSendPort
    plan_id: int
    seq: int
    #: true once the broker's HTTP request was handed to the transport
    started: bool = False
    refused_code: str | None = None
    closed: bool = False
    group: Any = None
    request: fence.FenceRequest | None = None
    now_fn: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC), repr=False
    )
    _inner: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    def hook(
        self, inner: Callable[[], Awaitable[None]] | None
    ) -> Callable[[], Awaitable[None]]:
        self._inner = inner

        async def _hook() -> None:
            if self.started:
                # a lower layer retrying the same request: a fence allows one POST
                raise PreSendFreshnessError(("mandate_fence_single_post",))
            if self._inner is not None:
                await self._inner()
            assert self.request is not None
            # the freeze and the plan are judged as of the edge, not as of the fence
            request = replace(self.request, now=self.now_fn())
            refusal = await self.port.boundary_refusal(request, fenced_seq=self.seq)
            if refusal is not None:
                self.refused_code = refusal
                raise PreSendFreshnessError((refusal,))
            self.started = True

        return _hook

    async def close_unsent(self, reason: str) -> None:
        """Release after a proven non-send. Never after a request may have been sent."""
        if self.started or self.closed:
            return
        self.closed = True
        await self.port.release_fence(self.plan_id, self.seq, reason_code=reason)

    async def close_rejected(self) -> None:
        """Release after the broker's explicit rejection of the request."""
        if self.closed:
            return
        self.closed = True
        await self.port.release_fence(
            self.plan_id, self.seq, reason_code="explicit_rejection"
        )


async def commit_fence_for_rung(
    group: Any,
    rung: Any,
    preview: dict[str, Any] | None,
    *,
    now: datetime,
    port: MandateSendPort = DEFAULT_PORT,
) -> SendFence | fence.FenceOutcome:
    """Commit the human fence for this rung, or return the refusal (no broker call)."""
    notional = fence_notional(rung, preview)
    if notional is None:
        return fence.FenceOutcome(False, refusal=v.MandateRefusal.UNREADABLE.value)
    try:
        account_ref = await port.account_ref_resolver(group)
    except Exception:  # noqa: BLE001
        account_ref = None
    request = _request(group, notional, account_ref, now, "human")
    outcome = await port.commit_fence(request)
    if not outcome.fenced or outcome.seq is None:
        return outcome
    return SendFence(
        port=port,
        plan_id=request.plan_id,
        seq=outcome.seq,
        group=group,
        request=request,
    )


@dataclass
class LossCutGuardHook:
    """The I14 recheck at the broker's HTTP edge for a ``loss_cut`` send (§5.2).

    Runs the proposal's own pre-send hook first, then the guard. A refusal is raised
    as a pre-send refusal so no request is made; it only ever adds a refusal.
    """

    inner: Callable[[], Awaitable[None]] | None
    refusal_fn: Callable[[], Awaitable[str | None]]
    refused_code: str | None = None

    async def __call__(self) -> None:
        if self.inner is not None:
            await self.inner()
        try:
            code = await self.refusal_fn()
        except Exception:  # noqa: BLE001 - an unreadable plan refuses the sale
            code = v.MandateRefusal.UNREADABLE.value
        if code is not None:
            self.refused_code = code
            raise PreSendFreshnessError((code,))
