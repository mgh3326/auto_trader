"""Fresh re-validation + submit orchestration for order_proposals (ROB-816 PR-2).

``revalidate_and_submit`` is the click-time orchestrator: it re-runs the full
guard chain (loss-sell, market-sell-loss, sector cap) via a fresh dry-run
preview, mints a brand-new ``approval_hash`` (age ~0s — this is the
server-internalized TTL that resolves the human-round-trip concern from
ROB-815), compares the normalized wire price/qty against what the operator
approved, and only submits when nothing has moved. All writes go through
``OrderProposalsService`` — this module never touches the DB directly and
never calls ``commit()`` (the caller owns the transaction, per the
service-layer convention in ``service.py``).

Principle #6 (accepted != filled): a broker ACK/resting confirmation is
recorded via ``record_ack``/``record_resting`` — never as a fill.
``record_fill_evidence`` is out of scope here; fills are booked later from
broker evidence (reconcile), not from this click-time path.

Principle #4 (never auto-void on ambiguity): a submit that raises, times out,
or returns a response this orchestrator cannot classify is recorded via
``record_unverified`` — never a terminal state.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.core.timezone import now_kst
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.live_correlation import live_correlation_id
from app.services.order_proposals.service import OrderProposalsService

RungOutcomeResult = Literal[
    "submitted_acked",
    "submitted_resting",
    "needs_reconfirm",
    "guard_blocked",
    "unverified",
    "error",
]


@dataclass
class RungOutcome:
    rung_index: int
    result: RungOutcomeResult
    detail: dict[str, Any]


PlaceOrderFn = Callable[..., Any]
CorrelationMint = Callable[..., Any]

_PREVIEW_REASON = "order_proposal revalidation (rung {rung})"
_SUBMIT_REASON = "order_proposal submit after revalidation (rung {rung})"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _norm(value: Any) -> str | None:
    """Canonical string form for a price/quantity value.

    ``rung.limit_price``/``rung.quantity`` round-trip through a
    ``NUMERIC(38, 12)`` column, so a freshly-fetched value like
    ``Decimal("2226000.000000000000")`` must compare equal (and render
    identically in the reconfirm diff) to a dry-run preview's
    ``Decimal("2226000")``/"2226000" — Postgres pads to the declared scale,
    the preview does not. Strip both to the same canonical fixed-point form
    (no scientific notation, no trailing zeros) before comparing.
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception:
            return str(value)
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


async def _default_place_order_fn(**kwargs: Any) -> dict[str, Any]:
    """Production binding — delegates to the real order-execution impl.

    Not exercised by this task's test suite (every test injects a fake
    ``place_order_fn``; real broker/httpx calls are always mocked in tests
    per the ROB-816 global constraints). Known gap: the live-submit
    (``dry_run=False``) response shape returned by ``_execute_and_record``
    inside ``_place_order_impl`` has not been confirmed to carry the
    ``{status, broker_order_id, correlation_id, idempotency_key,
    approval_hash_digest}`` contract this orchestrator classifies against —
    see the Task 13 report for the follow-up needed before this default
    binding can be trusted against a real broker response.
    """
    from app.mcp_server.tooling.order_execution import _place_order_impl

    return await _place_order_impl(**kwargs)


def _default_correlation_mint(
    *, group: OrderProposal, rung: OrderProposalRung, now: datetime
) -> str:
    return live_correlation_id(
        account_scope=group.account_mode,
        symbol=group.symbol,
        side=rung.side,
        price=rung.limit_price if rung.limit_price is not None else Decimal("0"),
        quantity=rung.quantity,
        kst_trade_day=now_kst().strftime("%Y-%m-%d"),
        rung=rung.rung_index,
    )


async def revalidate_and_submit(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    now: datetime,
    place_order_fn: PlaceOrderFn = _default_place_order_fn,
    correlation_mint: CorrelationMint = _default_correlation_mint,
) -> list[RungOutcome]:
    """Revalidate + (maybe) submit every ``pending_approval`` rung.

    Rungs not currently in ``pending_approval`` (already submitted, already
    terminal, etc.) are skipped — they are not eligible to re-enter the
    revalidation cycle from this call.
    """
    group, rungs = await service.get_proposal(proposal_id)
    outcomes: list[RungOutcome] = []
    for rung in rungs:
        if rung.state != "pending_approval":
            continue
        outcomes.append(
            await _revalidate_rung(
                service=service,
                group=group,
                rung=rung,
                now=now,
                place_order_fn=place_order_fn,
                correlation_mint=correlation_mint,
            )
        )
    return outcomes


async def _revalidate_rung(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    place_order_fn: PlaceOrderFn,
    correlation_mint: CorrelationMint,
) -> RungOutcome:
    proposal_id = group.proposal_id
    rung_index = rung.rung_index

    await service.transition_rung(proposal_id, rung_index, new_state="revalidating")

    preview = await _maybe_await(
        place_order_fn(
            dry_run=True,
            symbol=group.symbol,
            side=rung.side,
            market=group.market,
            order_type=group.order_type,
            quantity=rung.quantity,
            price=rung.limit_price,
            thesis=group.thesis,
            strategy=group.strategy,
            reason=_PREVIEW_REASON.format(rung=rung_index),
            rung=rung_index,
        )
    )

    if preview.get("success") is False:
        # Fail-closed: the guard chain (loss-sell / market-sell-loss / sector
        # cap) blocked this rung. Retryable — back to pending_approval, never
        # submitted.
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return RungOutcome(
            rung_index,
            "guard_blocked",
            {"error": preview.get("error"), "preview": preview},
        )

    before = {
        "limit_price": _norm(rung.limit_price),
        "quantity": _norm(rung.quantity),
    }
    after = {
        "limit_price": _norm(preview.get("price")),
        "quantity": _norm(preview.get("quantity")),
    }
    if before != after:
        await service.mark_needs_reconfirm(proposal_id, rung_index, now=now)
        return RungOutcome(
            rung_index, "needs_reconfirm", {"before": before, "after": after}
        )

    await service.transition_rung(proposal_id, rung_index, new_state="approved")
    await service.transition_rung(proposal_id, rung_index, new_state="submitting")

    corr = await _maybe_await(correlation_mint(group=group, rung=rung, now=now))

    try:
        submit = await _maybe_await(
            place_order_fn(
                dry_run=False,
                symbol=group.symbol,
                side=rung.side,
                market=group.market,
                order_type=group.order_type,
                quantity=rung.quantity,
                price=rung.limit_price,
                thesis=group.thesis,
                strategy=group.strategy,
                reason=_SUBMIT_REASON.format(rung=rung_index),
                approval_hash=preview.get("approval_hash"),
                rung=rung_index,
                correlation_id=corr,
            )
        )
    except Exception as exc:  # noqa: BLE001 - broker call; ambiguous, not a void
        await service.record_unverified(
            proposal_id, rung_index, reason=f"submit_exception:{exc}", now=now
        )
        return RungOutcome(rung_index, "unverified", {"error": str(exc)})

    return await _classify_submit(
        service=service,
        proposal_id=proposal_id,
        rung_index=rung_index,
        preview=preview,
        submit=submit,
        corr=corr,
        now=now,
    )


async def _classify_submit(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    rung_index: int,
    preview: dict[str, Any],
    submit: dict[str, Any],
    corr: str,
    now: datetime,
) -> RungOutcome:
    success = submit.get("success")

    if success is False:
        # Explicit broker/guard rejection — not ambiguous, safe to terminalize.
        await service.record_rejected(
            proposal_id,
            rung_index,
            reason=str(submit.get("error") or "submit_rejected"),
            now=now,
        )
        return RungOutcome(rung_index, "error", {"error": submit.get("error")})

    if success is True:
        status = submit.get("status")
        broker_order_id = submit.get("broker_order_id")
        if broker_order_id is not None and status in ("acked", "resting"):
            correlation_id = submit.get("correlation_id") or corr
            idempotency_key = submit.get("idempotency_key") or preview.get(
                "idempotency_key"
            )
            approval_hash_digest = submit.get("approval_hash_digest") or preview.get(
                "approval_hash"
            )
            record_fn = (
                service.record_ack if status == "acked" else service.record_resting
            )
            await record_fn(
                proposal_id,
                rung_index,
                broker_order_id=broker_order_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                approval_hash_digest=approval_hash_digest,
                now=now,
            )
            result: RungOutcomeResult = (
                "submitted_acked" if status == "acked" else "submitted_resting"
            )
            return RungOutcome(rung_index, result, {"submit": submit})

    # success is True but unrecognized status/missing broker_order_id, or
    # success is missing entirely (neither True nor False) — ambiguous.
    # Never auto-void on ambiguity (Principle #4).
    await service.record_unverified(
        proposal_id,
        rung_index,
        reason=f"ambiguous_submit_response:status={submit.get('status')!r}",
        now=now,
    )
    return RungOutcome(rung_index, "unverified", {"submit": submit})
