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

import hashlib
import inspect
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.core.timezone import now_kst
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.live_correlation import live_correlation_id
from app.services.order_proposals.broker_gateway import (
    cancel_target_order,
    fetch_submit_evidence,
    fetch_target_order,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.service import OrderProposalsService
from app.services.order_proposals.target_order import TargetOrderSnapshot

RungOutcomeResult = Literal[
    "submitted_acked",
    "submitted_resting",
    "needs_reconfirm",
    "guard_blocked",
    "unverified",
    "error",
    "cancelled",
]


@dataclass
class RungOutcome:
    rung_index: int
    result: RungOutcomeResult
    detail: dict[str, Any]


PlaceOrderFn = Callable[..., Any]
CorrelationMint = Callable[..., Any]
TargetFetchFn = Callable[..., Any]
TargetCancelFn = Callable[..., Any]
SubmitEvidenceFetchFn = Callable[..., Any]
RetrospectiveLookupFn = Callable[[int], Any]

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
_TOSS_CLIENT_ORDER_ID_MAX_LENGTH = 36
_TOSS_CLIENT_ORDER_ID_PATTERN = re.compile(r"[a-zA-Z0-9\-_]+")
_SUBMIT_DIAGNOSTIC_MAX_LENGTH = 240


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


def _toss_proposal_client_order_id(proposal_id: uuid.UUID, rung_index: int) -> str:
    """Return a proposal/rung-stable, Toss-safe private idempotency key."""
    digest = hashlib.sha256(f"{proposal_id}:{rung_index}".encode()).hexdigest()[:24]
    return f"tosprop-{digest}"


def _is_valid_toss_client_order_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _TOSS_CLIENT_ORDER_ID_MAX_LENGTH
        and _TOSS_CLIENT_ORDER_ID_PATTERN.fullmatch(value) is not None
    )


def _proposal_client_order_id(proposal_id: uuid.UUID, rung_index: int) -> str:
    digest = hashlib.sha256(f"{proposal_id}:{rung_index}".encode()).hexdigest()[:32]
    return f"oprop-{digest}"


def _truncate_submit_diagnostic(value: object) -> str:
    compact = " ".join(str(value).split())
    if len(compact) > _SUBMIT_DIAGNOSTIC_MAX_LENGTH:
        return compact[: _SUBMIT_DIAGNOSTIC_MAX_LENGTH - 1] + "…"
    return compact


def _toss_submit_error_summary(submit: dict[str, Any]) -> str | None:
    code = str(submit.get("code") or "").strip()
    if not code:
        return None
    message = str(submit.get("message") or "").strip()
    return _truncate_submit_diagnostic(
        f"Toss {code}: {message}" if message else f"Toss {code}"
    )


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


def _invalid_toss_preview_reason(
    preview: dict[str, Any], *, expected_client_order_id: str, order_type: str
) -> str | None:
    if preview.get("success") is not True:
        return "success_not_true"
    payload = preview.get("payload_preview")
    if not isinstance(payload, dict):
        return "payload_preview_missing_or_malformed"
    approval_hash = preview.get("approval_hash")
    if not isinstance(approval_hash, str) or not approval_hash.strip():
        return "approval_hash_missing"
    client_order_id = payload.get("clientOrderId")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        return "client_order_id_missing"
    if client_order_id != expected_client_order_id:
        return "client_order_id_mismatch"
    quantity = payload.get("quantity")
    if quantity is None or not str(quantity).strip():
        return "quantity_missing"
    if order_type == "limit":
        price = payload.get("price")
        if price is None or not str(price).strip():
            return "limit_price_missing"
    return None


def _adapt_toss_submit_response(
    submit: dict[str, Any], *, order_type: str
) -> dict[str, Any]:
    """Translate Toss accepted-only sends to the proposal submit contract."""
    adapted = dict(submit)
    diagnostic = _toss_submit_error_summary(submit)
    if diagnostic is not None:
        adapted["error"] = diagnostic

    if submit.get("success") is False and submit.get("mutation_sent") is True:
        status_code = submit.get("status_code")
        typed_client_rejection = (
            isinstance(status_code, int)
            and 400 <= status_code < 500
            and status_code != 429
            and bool(str(submit.get("code") or "").strip())
        )
        if typed_client_rejection:
            adapted["broker_status"] = "rejected"
        elif submit.get("broker_status") != "rejected":
            adapted["success"] = None
            return adapted
    if adapted.get("success") is False or adapted.get("broker_status") == "rejected":
        adapted["success"] = False
        adapted["error"] = (
            adapted.get("error")
            or adapted.get("response_message")
            or adapted.get("message")
            or "toss_order_rejected"
        )
        return adapted
    if (
        adapted.get("success") is not True
        or adapted.get("order_id") is None
        or adapted.get("approval_hash_digest") is None
    ):
        return adapted
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
    proposal_client_order_id = kwargs.pop("proposal_client_order_id", None)
    if account_mode == "toss_live":
        from app.mcp_server.tooling.orders_toss_variants import (
            _bind_order_proposal_context,
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
            "exit_intent": kwargs.get("exit_intent"),
            "exit_reason": kwargs.get("exit_reason"),
            "retrospective_id": kwargs.get("retrospective_id"),
            "approval_issue_id": kwargs.get("approval_issue_id"),
        }
        if kwargs.get("dry_run") is True:
            with _bind_order_proposal_context(
                client_order_id=str(proposal_client_order_id),
                correlation_id=None,
                rung=kwargs.get("rung"),
            ):
                preview = await toss_preview_order(**toss_kwargs)
            return _adapt_toss_preview_response(preview)

        approval_hash = kwargs.get("approval_hash")
        if not _is_valid_toss_client_order_id(proposal_client_order_id):
            return {
                "success": False,
                "mutation_sent": False,
                "error_code": "invalid_toss_client_order_id",
                "error": (
                    "Toss clientOrderId must be at most 36 characters and match "
                    "[a-zA-Z0-9\\-_]+"
                ),
            }
        with _bind_order_proposal_context(
            client_order_id=str(proposal_client_order_id),
            correlation_id=kwargs.get("correlation_id"),
            rung=kwargs.get("rung"),
        ):
            submit = await toss_place_order(
                **toss_kwargs,
                dry_run=False,
                confirm=True,
                approval_hash=approval_hash,
                reason=kwargs.get("reason"),
                thesis=kwargs.get("thesis"),
                strategy=kwargs.get("strategy"),
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
    if proposal_client_order_id is not None:
        kwargs["client_order_id"] = str(proposal_client_order_id)

    submit = await _place_order_impl(**kwargs, proposal_flow=True)
    if kwargs.get("dry_run") is False:
        return _adapt_live_submit_response(
            submit, order_type=str(kwargs.get("order_type"))
        )
    return submit


async def _default_retrospective_lookup(retrospective_id: int) -> Any:
    from app.mcp_server.tooling.order_validation import (
        _get_retrospective_by_id_for_loss_cut,
    )

    return await _get_retrospective_by_id_for_loss_cut(retrospective_id)


async def preview_loss_cut_confirmation(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    now: datetime,
    place_order_fn: PlaceOrderFn = _default_place_order_fn,
    retrospective_lookup_fn: RetrospectiveLookupFn = _default_retrospective_lookup,
) -> dict[str, Any]:
    """Build read-only, fresh evidence for Telegram's loss-cut second step."""
    group, rungs = await service.get_proposal(proposal_id)
    if group.exit_intent != "loss_cut" or group.retrospective_id is None:
        raise OrderProposalError("loss_cut_confirmation_requires_loss_cut")
    retrospective = await _maybe_await(retrospective_lookup_fn(group.retrospective_id))
    if retrospective is None:
        raise OrderProposalError("loss_cut_confirmation_retrospective_missing")

    evidence_rungs: list[dict[str, Any]] = []
    for rung in rungs:
        if rung.state not in {"pending_approval", "needs_reconfirm"}:
            continue
        proposal_client_order_id = (
            _toss_proposal_client_order_id(proposal_id, rung.rung_index)
            if group.account_mode == "toss_live"
            else _proposal_client_order_id(proposal_id, rung.rung_index)
            if group.account_mode == "upbit"
            else None
        )
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
                reason=_PREVIEW_REASON.format(rung=rung.rung_index),
                rung=rung.rung_index,
                **(
                    {"proposal_client_order_id": proposal_client_order_id}
                    if proposal_client_order_id is not None
                    else {}
                ),
            )
        )
        if preview.get("success") is False:
            raise OrderProposalError(
                str(preview.get("error") or "loss_cut_confirmation_preview_blocked")
            )
        try:
            current_price = Decimal(str(preview["current_price"]))
            avg_buy_price = Decimal(str(preview["avg_buy_price"]))
            slip_band = Decimal(str(preview["loss_cut_slip_band"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderProposalError(
                "loss_cut_confirmation_preview_missing_price_context"
            ) from exc
        if avg_buy_price <= 0:
            raise OrderProposalError("loss_cut_confirmation_invalid_average_cost")
        loss_pct = ((current_price - avg_buy_price) / avg_buy_price * 100).quantize(
            Decimal("0.01")
        )
        evidence_rungs.append(
            {
                "rung_index": rung.rung_index,
                "current_price": _norm(current_price),
                "avg_buy_price": _norm(avg_buy_price),
                "loss_pct": format(loss_pct, "f"),
                "loss_cut_slip_band": _norm(slip_band),
            }
        )
    if not evidence_rungs:
        raise OrderProposalError("loss_cut_confirmation_has_no_eligible_rungs")
    lesson = " ".join(str(getattr(retrospective, "lesson", None) or "미기재").split())
    if len(lesson) > 240:
        lesson = lesson[:239] + "…"
    return {
        "rungs": evidence_rungs,
        "retrospective_id": group.retrospective_id,
        "lesson_excerpt": lesson,
    }


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
    fetch_target_fn: TargetFetchFn = fetch_target_order,
    cancel_target_fn: TargetCancelFn = cancel_target_order,
    fetch_submit_evidence_fn: SubmitEvidenceFetchFn = fetch_submit_evidence,
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
        action = group.action or "place"
        if action == "replace":
            outcome = await _revalidate_replace_rung(
                service=service,
                group=group,
                rung=rung,
                now=now,
                place_order_fn=place_order_fn,
                fetch_target_fn=fetch_target_fn,
                cancel_target_fn=cancel_target_fn,
                correlation_mint=correlation_mint,
                fetch_submit_evidence_fn=fetch_submit_evidence_fn,
            )
        elif action == "cancel":
            outcome = await _revalidate_cancel_rung(
                service=service,
                group=group,
                rung=rung,
                now=now,
                fetch_target_fn=fetch_target_fn,
                cancel_target_fn=cancel_target_fn,
            )
        else:
            outcome = await _revalidate_place_rung(
                service=service,
                group=group,
                rung=rung,
                now=now,
                place_order_fn=place_order_fn,
                correlation_mint=correlation_mint,
                fetch_submit_evidence_fn=fetch_submit_evidence_fn,
            )
        outcomes.append(outcome)
    return outcomes


async def _revalidate_place_rung(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    place_order_fn: PlaceOrderFn,
    correlation_mint: CorrelationMint,
    fetch_submit_evidence_fn: SubmitEvidenceFetchFn,
) -> RungOutcome:
    proposal_id = group.proposal_id
    rung_index = rung.rung_index
    proposal_client_order_id = (
        _toss_proposal_client_order_id(proposal_id, rung_index)
        if group.account_mode == "toss_live"
        else _proposal_client_order_id(proposal_id, rung_index)
        if group.account_mode == "upbit"
        else None
    )

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
                **(
                    {"proposal_client_order_id": proposal_client_order_id}
                    if proposal_client_order_id is not None
                    else {}
                ),
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

    if group.account_mode == "toss_live":
        invalid_reason = _invalid_toss_preview_reason(
            preview,
            expected_client_order_id=proposal_client_order_id,
            order_type=group.order_type,
        )
        if invalid_reason is not None:
            await service.transition_rung(
                proposal_id, rung_index, new_state="pending_approval"
            )
            return RungOutcome(
                rung_index,
                "error",
                {
                    "error": f"invalid_toss_preview:{invalid_reason}",
                    "preview": preview,
                },
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
                **(
                    {"proposal_client_order_id": proposal_client_order_id}
                    if proposal_client_order_id is not None
                    else {}
                ),
                rung=rung_index,
                correlation_id=corr,
            )
        )
    except Exception as exc:  # noqa: BLE001 - broker call; ambiguous, not a void
        if group.account_mode == "upbit":
            return await _classify_submit(
                service=service,
                proposal_id=proposal_id,
                rung_index=rung_index,
                preview=preview,
                submit={"success": False, "error": str(exc)},
                corr=corr,
                now=now,
                account_mode=group.account_mode,
                market=group.market,
                identifier=proposal_client_order_id,
                fetch_submit_evidence_fn=fetch_submit_evidence_fn,
            )
        await service.record_unverified(
            proposal_id,
            rung_index,
            reason=f"submit_exception:{exc}",
            now=now,
            correlation_id=corr,
            idempotency_key=preview.get("idempotency_key"),
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
        account_mode=group.account_mode,
        market=group.market,
        identifier=proposal_client_order_id,
        fetch_submit_evidence_fn=fetch_submit_evidence_fn,
    )


def _target_mismatch_reason(
    approved: TargetOrderSnapshot,
    fresh: TargetOrderSnapshot,
) -> str | None:
    if fresh.status != "open":
        return f"target_not_open:{fresh.status}"
    if Decimal(fresh.remaining_quantity) <= 0:
        return "target_has_no_remaining_quantity"
    for field in (
        "broker_order_id",
        "symbol",
        "side",
        "order_type",
        "limit_price",
        "remaining_quantity",
    ):
        if getattr(approved, field) != getattr(fresh, field):
            return f"target_snapshot_mismatch:{field}"
    return None


async def _validate_target_action(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    fetch_target_fn: TargetFetchFn,
) -> RungOutcome | None:
    proposal_id = group.proposal_id
    rung_index = rung.rung_index
    await service.transition_rung(proposal_id, rung_index, new_state="revalidating")

    try:
        target_id = group.target_broker_order_id
        if target_id is None:
            raise ValueError("target_broker_order_id_missing")
        approved = TargetOrderSnapshot.from_payload(
            (group.source_asof or {})["target_order_snapshot"]
        )
    except Exception as exc:
        await service.record_rejected(
            proposal_id, rung_index, reason=f"target_evidence_invalid:{exc}", now=now
        )
        return RungOutcome(rung_index, "error", {"error": str(exc)})

    try:
        fresh = await _maybe_await(
            fetch_target_fn(
                order_id=target_id,
                symbol=group.symbol,
                market=group.market,
                account_mode=group.account_mode,
                now=now,
            )
        )
        mismatch_reason = _target_mismatch_reason(approved, fresh)
    except Exception as exc:
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return RungOutcome(rung_index, "error", {"error": f"target_fetch_error:{exc}"})

    if fresh is None:
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return RungOutcome(rung_index, "error", {"error": "target_evidence_missing"})

    if mismatch_reason is not None:
        await service.record_rejected(
            proposal_id, rung_index, reason=mismatch_reason, now=now
        )
        return RungOutcome(rung_index, "error", {"error": mismatch_reason})
    return None


async def _revalidate_replace_preview(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    place_order_fn: PlaceOrderFn,
    proposal_client_order_id: str | None,
) -> tuple[dict[str, Any] | None, RungOutcome | None]:
    proposal_id = group.proposal_id
    rung_index = rung.rung_index
    try:
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
                exit_intent=group.exit_intent,
                exit_reason=group.exit_reason,
                retrospective_id=group.retrospective_id,
                approval_issue_id=group.approval_issue_id,
                reason=_PREVIEW_REASON.format(rung=rung_index),
                rung=rung_index,
                account_mode=group.account_mode,
                **(
                    {"proposal_client_order_id": proposal_client_order_id}
                    if proposal_client_order_id is not None
                    else {}
                ),
            )
        )
    except Exception as exc:
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return None, RungOutcome(rung_index, "error", {"error": str(exc)})

    if preview.get("success") is False:
        await service.transition_rung(
            proposal_id, rung_index, new_state="pending_approval"
        )
        return None, RungOutcome(
            rung_index,
            "guard_blocked" if _is_guard_blocked_preview(preview) else "error",
            {"error": preview.get("error"), "preview": preview},
        )

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
        return None, RungOutcome(
            rung_index, "needs_reconfirm", {"before": before, "after": after}
        )
    return preview, None


async def _cancel_and_confirm_target(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    fetch_target_fn: TargetFetchFn,
    cancel_target_fn: TargetCancelFn,
) -> RungOutcome | None:
    proposal_id = group.proposal_id
    rung_index = rung.rung_index
    target_id = group.target_broker_order_id
    if target_id is None:
        await service.record_rejected(
            proposal_id, rung_index, reason="target_broker_order_id_missing", now=now
        )
        return RungOutcome(
            rung_index, "error", {"error": "target_broker_order_id_missing"}
        )

    try:
        cancel_result = await _maybe_await(
            cancel_target_fn(
                order_id=target_id,
                symbol=group.symbol,
                market=group.market,
                account_mode=group.account_mode,
            )
        )
    except Exception as exc:
        await service.record_unverified(
            proposal_id, rung_index, reason=f"cancel_exception:{exc}", now=now
        )
        return RungOutcome(rung_index, "unverified", {"error": str(exc)})

    if not isinstance(cancel_result, dict) or cancel_result.get("success") is not True:
        reason = (
            str(cancel_result.get("error") or "cancel_rejected")
            if isinstance(cancel_result, dict)
            else "cancel_rejected"
        )
        await service.record_rejected(proposal_id, rung_index, reason=reason, now=now)
        return RungOutcome(rung_index, "error", {"error": "cancel_rejected"})

    try:
        confirmed = await _maybe_await(
            fetch_target_fn(
                order_id=target_id,
                symbol=group.symbol,
                market=group.market,
                account_mode=group.account_mode,
                now=now,
            )
        )
        confirmed_status = getattr(confirmed, "status", None)
    except Exception as exc:
        await service.record_unverified(
            proposal_id,
            rung_index,
            reason=f"cancel_confirmation_error:{exc}",
            now=now,
        )
        return RungOutcome(rung_index, "unverified", {"error": str(exc)})

    if confirmed is None:
        await service.record_unverified(
            proposal_id,
            rung_index,
            reason="cancel_confirmation_missing_evidence",
            now=now,
        )
        return RungOutcome(
            rung_index, "unverified", {"error": "cancel_confirmation_missing_evidence"}
        )
    if confirmed_status != "cancelled":
        await service.record_unverified(
            proposal_id,
            rung_index,
            reason=f"cancel_unconfirmed:{confirmed_status}",
            now=now,
        )
        return RungOutcome(rung_index, "unverified", {"error": "cancel_unconfirmed"})
    return None


async def _revalidate_replace_rung(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    place_order_fn: PlaceOrderFn,
    fetch_target_fn: TargetFetchFn,
    cancel_target_fn: TargetCancelFn,
    correlation_mint: CorrelationMint,
    fetch_submit_evidence_fn: SubmitEvidenceFetchFn,
) -> RungOutcome:
    proposal_client_order_id = (
        _proposal_client_order_id(group.proposal_id, rung.rung_index)
        if group.account_mode == "upbit"
        else None
    )
    target_outcome = await _validate_target_action(
        service=service,
        group=group,
        rung=rung,
        now=now,
        fetch_target_fn=fetch_target_fn,
    )
    if target_outcome is not None:
        return target_outcome

    preview, preview_outcome = await _revalidate_replace_preview(
        service=service,
        group=group,
        rung=rung,
        now=now,
        place_order_fn=place_order_fn,
        proposal_client_order_id=proposal_client_order_id,
    )
    if preview_outcome is not None:
        return preview_outcome

    proposal_id = group.proposal_id
    rung_index = rung.rung_index
    await service.transition_rung(proposal_id, rung_index, new_state="approved")
    await service.transition_rung(proposal_id, rung_index, new_state="submitting")
    cancel_outcome = await _cancel_and_confirm_target(
        service=service,
        group=group,
        rung=rung,
        now=now,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )
    if cancel_outcome is not None:
        return cancel_outcome

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
                exit_intent=group.exit_intent,
                exit_reason=group.exit_reason,
                retrospective_id=group.retrospective_id,
                approval_issue_id=group.approval_issue_id,
                reason=_SUBMIT_REASON.format(rung=rung_index),
                approval_hash=preview.get("approval_hash"),
                rung=rung_index,
                correlation_id=corr,
                account_mode=group.account_mode,
                **(
                    {"proposal_client_order_id": proposal_client_order_id}
                    if proposal_client_order_id is not None
                    else {}
                ),
            )
        )
    except Exception as exc:
        if group.account_mode == "upbit":
            return await _classify_submit(
                service=service,
                proposal_id=proposal_id,
                rung_index=rung_index,
                preview=preview,
                submit={"success": False, "error": str(exc)},
                corr=corr,
                now=now,
                account_mode=group.account_mode,
                market=group.market,
                identifier=proposal_client_order_id,
                fetch_submit_evidence_fn=fetch_submit_evidence_fn,
            )
        await service.record_unverified(
            proposal_id,
            rung_index,
            reason=f"submit_exception:{exc}",
            now=now,
            correlation_id=corr,
            idempotency_key=preview.get("idempotency_key"),
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
        account_mode=group.account_mode,
        market=group.market,
        identifier=proposal_client_order_id,
        fetch_submit_evidence_fn=fetch_submit_evidence_fn,
    )


async def _revalidate_cancel_rung(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rung: OrderProposalRung,
    now: datetime,
    fetch_target_fn: TargetFetchFn,
    cancel_target_fn: TargetCancelFn,
) -> RungOutcome:
    target_outcome = await _validate_target_action(
        service=service,
        group=group,
        rung=rung,
        now=now,
        fetch_target_fn=fetch_target_fn,
    )
    if target_outcome is not None:
        return target_outcome

    proposal_id = group.proposal_id
    rung_index = rung.rung_index
    await service.transition_rung(proposal_id, rung_index, new_state="approved")
    await service.transition_rung(proposal_id, rung_index, new_state="submitting")
    cancel_outcome = await _cancel_and_confirm_target(
        service=service,
        group=group,
        rung=rung,
        now=now,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )
    if cancel_outcome is not None:
        return cancel_outcome

    target_id = group.target_broker_order_id
    if target_id is None:
        raise AssertionError("target id validated before cancellation")
    await service.record_cancelled(
        proposal_id, rung_index, broker_order_id=target_id, now=now
    )
    return RungOutcome(rung_index, "cancelled", {})


async def _classify_submit(
    *,
    service: OrderProposalsService,
    proposal_id: uuid.UUID,
    rung_index: int,
    preview: dict[str, Any],
    submit: dict[str, Any],
    corr: str,
    now: datetime,
    account_mode: str,
    market: str,
    identifier: str | None,
    fetch_submit_evidence_fn: SubmitEvidenceFetchFn,
) -> RungOutcome:
    success = submit.get("success")

    if success is False:
        original_error = str(submit.get("error") or "submit_rejected")
        if account_mode == "upbit" and identifier is not None:
            evidence = await _maybe_await(
                fetch_submit_evidence_fn(
                    identifier=identifier,
                    account_mode=account_mode,
                    market=market,
                )
            )
            if evidence.outcome == "found":
                status = (
                    "resting" if evidence.broker_state in {"wait", "watch"} else "acked"
                )
                record_fn = (
                    service.record_resting
                    if status == "resting"
                    else service.record_ack
                )
                await record_fn(
                    proposal_id,
                    rung_index,
                    broker_order_id=evidence.broker_order_id,
                    correlation_id=corr,
                    idempotency_key=identifier,
                    approval_hash_digest=preview.get("approval_hash"),
                    now=now,
                )
                result: RungOutcomeResult = (
                    "submitted_resting" if status == "resting" else "submitted_acked"
                )
                return RungOutcome(
                    rung_index,
                    result,
                    {"submit": submit, "submit_evidence": evidence},
                )
            if evidence.outcome == "unknown":
                await service.record_unverified(
                    proposal_id,
                    rung_index,
                    reason=(
                        f"submit_evidence_unknown:{evidence.reason or original_error}"
                    ),
                    now=now,
                    correlation_id=corr,
                    idempotency_key=identifier,
                )
                return RungOutcome(
                    rung_index,
                    "unverified",
                    {"error": original_error, "submit_evidence": evidence},
                )
        # Explicit broker/guard rejection — not ambiguous, safe to terminalize.
        await service.record_rejected(
            proposal_id,
            rung_index,
            reason=original_error,
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
    ambiguous_diagnostic = _truncate_submit_diagnostic(
        submit.get("error") or f"status={submit.get('status')!r}"
    )
    await service.record_unverified(
        proposal_id,
        rung_index,
        reason=f"ambiguous_submit_response:{ambiguous_diagnostic}",
        now=now,
        correlation_id=corr,
        idempotency_key=(
            identifier
            or submit.get("idempotency_key")
            or preview.get("idempotency_key")
        ),
    )
    return RungOutcome(rung_index, "unverified", {"submit": submit})
