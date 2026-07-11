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
_GUARD_ERROR_CODES = frozenset(
    {"loss_cut_preconditions_failed", "nxt_session_not_tradable"}
)
_GUARD_ERROR_MARKERS = (
    "loss_sell_blocked",
    "sell price ",
    "live market sell blocked",
    "loss_cut sell price",
    "no holdings found",
    "no sellable holdings",
    "insufficient ",
    "stop-loss cooldown",
    "opposite pending order",
)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _is_guard_blocked_preview(preview: dict[str, Any]) -> bool:
    if preview.get("success") is not False:
        return False
    if preview.get("insufficient_balance") is True or preview.get("violations"):
        return True
    error_code = str(preview.get("error_code") or "").lower()
    error = str(preview.get("error") or "").lower()
    return error_code in _GUARD_ERROR_CODES or any(
        marker in error for marker in _GUARD_ERROR_MARKERS
    )


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


def _toss_decimal_arg(value: Decimal | None) -> str | int | None:
    """Convert proposal numerics to Toss's exact ``str | int`` contract."""
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return format(normalized, "f")


def _adapt_toss_preview_response(preview: dict[str, Any]) -> dict[str, Any]:
    """Expose Toss's normalized wire payload through the proposal contract."""
    payload = preview.get("payload_preview")
    if not isinstance(payload, dict):
        return preview
    adapted = dict(preview)
    adapted["price"] = payload.get("price")
    adapted["quantity"] = payload.get("quantity")
    adapted["idempotency_key"] = payload.get("clientOrderId")
    return adapted


def _adapt_toss_submit_response(
    submit: dict[str, Any], *, order_type: str
) -> dict[str, Any]:
    """Translate Toss accepted-only sends to the proposal submit contract."""
    if submit.get("success") is False or submit.get("broker_status") == "rejected":
        adapted = dict(submit)
        adapted["success"] = False
        adapted["error"] = (
            submit.get("error")
            or submit.get("response_message")
            or submit.get("message")
            or "toss_order_rejected"
        )
        return adapted
    if (
        submit.get("success") is not True
        or submit.get("order_id") is None
        or submit.get("approval_hash_digest") is None
    ):
        return submit
    adapted = dict(submit)
    adapted["status"] = "acked" if order_type == "market" else "resting"
    adapted["broker_order_id"] = submit.get("order_id")
    adapted["idempotency_key"] = submit.get("client_order_id")
    return adapted


def _adapt_live_submit_response(
    submit: dict[str, Any], *, order_type: str
) -> dict[str, Any]:
    """Translate a real live-submit response into ``_classify_submit``'s shape.

    The real ``_place_order_impl(dry_run=False)`` response (see
    ``_record_kis_live_order`` in ``kis_live_ledger.py`` and
    ``_record_live_order`` in ``live_order_ledger.py``) is accepted-only at
    send: it carries ``broker_status in ("accepted", "rejected")`` and
    ``order_id``/``correlation_id`` — never a synchronous acked-vs-resting
    distinction, because that distinction doesn't exist in the broker's
    real-time API contract (it's an order_proposals-internal concept; fills
    are booked later by reconcile from broker evidence, per the KIS Live
    Order Fill-Evidence Gate / US & Crypto Live Order Fill-Evidence Gate
    design). Adapt the real shape into ``{status, broker_order_id}`` here so
    ``_classify_submit``'s existing tested contract stays untouched.
    """
    broker_status = submit.get("broker_status")
    if broker_status == "rejected":
        adapted = dict(submit)
        adapted["success"] = False
        adapted["error"] = (
            submit.get("response_message") or submit.get("message") or "broker_rejected"
        )
        return adapted
    if broker_status == "accepted":
        adapted = dict(submit)
        adapted["broker_order_id"] = submit.get("order_id")
        adapted["status"] = "acked" if order_type == "market" else "resting"
        return adapted
    # Defensive: unknown/missing broker_status shouldn't happen given
    # `_derive_live_send_status` only ever returns "accepted"/"rejected", but
    # leave the response untouched rather than raise — `_classify_submit`'s
    # existing ambiguous-response fallback (`record_unverified`) still
    # applies to whatever shape falls through here.
    return submit


async def _default_place_order_fn(**kwargs: Any) -> dict[str, Any]:
    """Production binding — delegates to the real order-execution impl.

    Not exercised by this task's test suite (every test injects a fake
    ``place_order_fn``; real broker/httpx calls are always mocked in tests
    per the ROB-816 global constraints). The dry-run preview response is
    passed through unchanged — ``_revalidate_rung`` already reads its real
    top-level keys (``price``/``quantity``/``success``/``approval_hash``).
    The live-submit (``dry_run=False``) response is translated via
    ``_adapt_live_submit_response`` before being handed to
    ``_classify_submit`` — see that helper's docstring and Task 13 report
    Finding 1 for why the raw response can't be classified directly.
    """
    account_mode = kwargs.pop("account_mode", None)
    client_order_id_override = kwargs.pop("client_order_id_override", None)
    if account_mode == "toss_live":
        from app.mcp_server.tooling.orders_toss_variants import (
            toss_place_order,
            toss_preview_order,
        )

        market = {"equity_kr": "kr", "equity_us": "us"}[str(kwargs["market"])]
        toss_kwargs = {
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "order_type": kwargs["order_type"],
            "quantity": _toss_decimal_arg(kwargs.get("quantity")),
            "price": _toss_decimal_arg(kwargs.get("price")),
            "market": market,
            "account_mode": account_mode,
            "rung": kwargs.get("rung"),
        }
        if kwargs.get("dry_run") is True:
            return _adapt_toss_preview_response(await toss_preview_order(**toss_kwargs))

        approval_hash = kwargs.get("approval_hash")
        submit = await toss_place_order(
            **toss_kwargs,
            dry_run=False,
            confirm=True,
            approval_hash=approval_hash,
            reason=kwargs.get("reason"),
            exit_reason=kwargs.get("exit_reason"),
            thesis=kwargs.get("thesis"),
            strategy=kwargs.get("strategy"),
            client_order_id_override=client_order_id_override,
        )
        return _adapt_toss_submit_response(
            submit,
            order_type=str(kwargs.get("order_type")),
        )

    from app.mcp_server.tooling.order_execution import _place_order_impl

    # The proposal ledger stores quantity/limit_price as Decimal, but
    # `_place_order_impl`'s numeric paths assume the MCP tool layer's
    # float/int inputs — e.g. `_preview_buy` computes the fee as
    # `estimated_value * 0.0005`, which raises TypeError on Decimal and
    # surfaced to the operator as a bogus "guard_blocked" (2026-07-11
    # activation smoke, KRW-BTC canary). Normalize at this caller boundary;
    # the impl's float contract stays unchanged for every other caller.
    kwargs = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in kwargs.items()}

    submit = await _place_order_impl(**kwargs)
    if kwargs.get("dry_run") is False:
        return _adapt_live_submit_response(
            submit, order_type=str(kwargs.get("order_type"))
        )
    return submit


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

    try:
        preview = await _maybe_await(
            place_order_fn(
                dry_run=True,
                account_mode=group.account_mode,
                symbol=group.symbol,
                side=rung.side,
                market=group.market,
                order_type=group.order_type,
                quantity=rung.quantity,
                price=rung.limit_price,
                thesis=group.thesis,
                strategy=group.strategy,
                exit_intent=group.exit_intent,
                exit_reason=group.exit_reason,
                retrospective_id=group.retrospective_id,
                approval_issue_id=group.approval_issue_id,
                reason=_PREVIEW_REASON.format(rung=rung_index),
                rung=rung_index,
            )
        )
    except Exception as exc:  # noqa: BLE001 - preview never reached the broker
        # Unlike a submit-phase exception, a preview-phase exception carries
        # no ambiguity about broker state (nothing was sent) — safe and
        # correct to make this retryable rather than parking it in a
        # non-revisitable holding state (see Task 13 review Finding 3).
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return RungOutcome(rung_index, "error", {"error": str(exc)})

    if preview.get("success") is False:
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return RungOutcome(
            rung_index,
            "guard_blocked" if _is_guard_blocked_preview(preview) else "error",
            {"error": preview.get("error"), "preview": preview},
        )

    # Market-order rungs have no `limit_price` by design (it's always None),
    # but `_build_preview` always backfills `preview["price"]` with the live
    # current price for market orders — comparing that against the stored
    # `None` would deterministically mismatch on every attempt. Degrade the
    # comparison to quantity-only for market-order rungs (Task 13 review
    # Finding 2); limit-order rungs keep the full price+quantity comparison.
    if rung.limit_price is None:
        before = {"limit_price": None, "quantity": _norm(rung.quantity)}
        after = {"limit_price": None, "quantity": _norm(preview.get("quantity"))}
    else:
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
                account_mode=group.account_mode,
                symbol=group.symbol,
                side=rung.side,
                market=group.market,
                order_type=group.order_type,
                quantity=rung.quantity,
                price=rung.limit_price,
                thesis=group.thesis,
                strategy=group.strategy,
                exit_intent=group.exit_intent,
                exit_reason=group.exit_reason,
                retrospective_id=group.retrospective_id,
                approval_issue_id=group.approval_issue_id,
                reason=_SUBMIT_REASON.format(rung=rung_index),
                approval_hash=preview.get("approval_hash"),
                client_order_id_override=(
                    preview.get("idempotency_key")
                    if group.account_mode == "toss_live"
                    else None
                ),
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
