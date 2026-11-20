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
from app.services.brokers.pre_send_account import PRE_SEND_ACCOUNT
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


async def _resolve_toss_identifier() -> str | None:
    """A Toss account the settings do not name: the client's own single-account
    resolution, the one the real request header will use (§4.6 "계좌 결속")."""
    from app.services.brokers.toss.client import TossReadClient

    client = TossReadClient.from_settings()
    try:
        return str(await client._resolve_account_seq())
    finally:
        await client.aclose()


async def resolve_actual_account_ref(group: object) -> str | None:
    """The reference of the account this send will use; ``None`` = cannot compute (block)."""
    mode = str(getattr(group, "account_mode", ""))
    identifier = actual_account_identifier(mode)
    if identifier is None and mode == "toss_live":
        try:
            identifier = await _resolve_toss_identifier()
        except Exception:  # noqa: BLE001 - an account that cannot be resolved blocks
            identifier = None
    if identifier is None:
        return None
    return derive_account_ref(mode, identifier, secret=settings.SECRET_KEY)


def edge_account_ref(group: object, fallback: str | None) -> str | None:
    """The reference of the account of the prepared request, when the transport says."""
    published = PRE_SEND_ACCOUNT.get()
    if published is None:
        return fallback
    return derive_account_ref(
        str(getattr(group, "account_mode", "")),
        published,
        secret=settings.SECRET_KEY,
    )


def is_affirmative_rejection(submit: Any) -> bool:
    """The broker's own, typed refusal: only this (or a proven non-send) closes a fence.

    A failure that says the outcome is unknown (a timeout after the request left),
    or that carries neither a broker status nor an error code, proves nothing.
    """
    if not isinstance(submit, dict) or submit.get("success") is not False:
        return False
    if submit.get("outcome_unknown") is True:
        return False
    if (
        submit.get("broker_status") == "rejected"
        or submit.get("ledger_status") == "rejected"
        or submit.get("mutation_sent") is False
        or submit.get("status") == "not_submitted"
    ):
        return True
    code = submit.get("error_code")
    return isinstance(code, str) and bool(code)


def approver_refusal(principal: str | None) -> str | None:
    """§3.5/§4.7: only a listed operator principal may spend a plan-bound card."""
    allowed = {
        item.strip() for item in settings.MANDATE_APPROVER_TELEGRAM_USER_IDS.split(",")
    }
    if principal is None or str(principal) not in allowed:
        return "mandate_approver_not_allowed"
    return None


def legacy_auto_dispatch_refusal(group: object) -> str | None:
    """§4.6 deployment order (1): no live plan send while the legacy dispatch runs."""
    try:
        live = v.realm_of(str(getattr(group, "account_mode", ""))) == "live"
    except ValueError:
        return None
    if live and settings.ORDER_PROPOSALS_AUTO_APPROVE:
        return "mandate_auto_dispatch_active"
    return None


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
        # a provider reports its fresh quote under its own key (Kiwoom KR mock:
        # ``estimated_evidence.quote_price``); the larger of all of them counts
        evidence = preview.get("estimated_evidence")
        candidates = [
            preview.get("current_price"),
            evidence.get("quote_price") if isinstance(evidence, dict) else None,
        ]
        for raw in candidates:
            try:
                current = Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
            if current.is_finite() and current > price:
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


def unsupported_shape_refusal(
    group: Any, rungs: list[Any], *, states: tuple[str, ...] = ("pending_approval",)
) -> str | None:
    """A plan-bound proposal is one resting buy limit rung (§4.1): anything else is refused.

    ``states`` are the rung states that count as the one eligible rung: the send path
    needs ``pending_approval``; the pre-nonce check also accepts ``needs_reconfirm``
    (a reconfirmation card the callback promotes back to ``pending_approval``).
    """
    if (group.action or "place") != SUPPORTED_ACTION:
        return v.MandateRefusal.UNSUPPORTED.value
    eligible = [r for r in rungs if r.state in states]
    if len(eligible) != 1 or eligible[0].side != SUPPORTED_SIDE:
        return v.MandateRefusal.BINDING_MISMATCH.value
    return None


async def pre_nonce_refusal(
    group: Any,
    rungs: list[Any],
    *,
    now: datetime,
    principal: str | None = None,
    port: MandateSendPort = DEFAULT_PORT,
) -> str | None:
    """A refusal before the nonce is spent, or ``None``. Advice only (§4.7).

    The operator principal check is authority, not advice: an unlisted sender never
    spends a plan-bound card. The rest keeps a send that is going to be refused from
    using up the approval; the fence transaction decides alone later.
    """
    if not is_plan_bound(group):
        return None
    principal_block = approver_refusal(principal)
    if principal_block is not None:
        return principal_block
    pilot = legacy_auto_dispatch_refusal(group)
    if pilot is not None:
        return pilot
    shape = unsupported_shape_refusal(
        group, rungs, states=("pending_approval", "needs_reconfirm")
    )
    if shape is not None:
        return shape
    rung = next(r for r in rungs if r.state in ("pending_approval", "needs_reconfirm"))
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


async def pre_preview_refusal(
    group: Any,
    rung: Any,
    *,
    now: datetime,
    port: MandateSendPort = DEFAULT_PORT,
) -> str | None:
    """The send boundary's predicate right before the broker preview (§4.3 (나))."""
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
            assert self.request is not None
            # Every database read comes first; the proposal's own approval-window
            # check runs last, so a read that returns after valid_until cannot be
            # followed by a send. The account is the one of the prepared request.
            request = replace(
                self.request,
                now=self.now_fn(),
                actual_account_ref=edge_account_ref(
                    self.group, self.request.actual_account_ref
                ),
            )
            refusal = await self.port.boundary_refusal(request, fenced_seq=self.seq)
            if refusal is not None:
                self.refused_code = refusal
                raise PreSendFreshnessError((refusal,))
            if self._inner is not None:
                await self._inner()
            self.started = True

        return _hook

    async def close_unsent(self, reason: str) -> bool:
        """Release after a proven non-send; never after a request may have been sent.

        True only when the fence is committed closed (or already was). A failed
        release is logged with the fence it strands, so recovery can find it, and the
        fence stays open (the safe direction).
        """
        if self.started or self.closed:
            return self.closed
        outcome = await self.port.release_fence(
            self.plan_id, self.seq, reason_code=reason
        )
        self.closed = bool(outcome.released or outcome.reason == "already_closed")
        if not self.closed:
            logger.error(
                "mandate fence %s of plan %s was not released after a proven non-send (%s): %s",
                self.seq,
                self.plan_id,
                reason,
                outcome.reason,
            )
        return self.closed

    async def close_rejected(self) -> bool:
        """Release after the broker's explicit, typed rejection of the request."""
        if self.closed:
            return True
        outcome = await self.port.release_fence(
            self.plan_id, self.seq, reason_code="explicit_rejection"
        )
        self.closed = bool(outcome.released or outcome.reason == "already_closed")
        if not self.closed:
            logger.error(
                "mandate fence %s of plan %s was not released after a rejection: %s",
                self.seq,
                self.plan_id,
                outcome.reason,
            )
        return self.closed


async def commit_fence_for_rung(
    group: Any,
    rung: Any,
    preview: dict[str, Any] | None,
    *,
    now: datetime,
    port: MandateSendPort = DEFAULT_PORT,
) -> SendFence | fence.FenceOutcome:
    """Commit the human fence for this rung, or return the refusal (no broker call)."""
    pilot = legacy_auto_dispatch_refusal(group)
    if pilot is not None:
        return fence.FenceOutcome(False, refusal=pilot)
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

    Runs the guard first, then the proposal's own pre-send hook (the window check). A refusal is raised
    as a pre-send refusal so no request is made; it only ever adds a refusal.
    """

    inner: Callable[[], Awaitable[None]] | None
    refusal_fn: Callable[[], Awaitable[str | None]]
    refused_code: str | None = None

    async def __call__(self) -> None:
        try:
            code = await self.refusal_fn()
        except Exception:  # noqa: BLE001 - an unreadable plan refuses the sale
            code = v.MandateRefusal.UNREADABLE.value
        if code is not None:
            self.refused_code = code
            raise PreSendFreshnessError((code,))
        # the approval-window check is the last thing before the request, after
        # every database read
        if self.inner is not None:
            await self.inner()
