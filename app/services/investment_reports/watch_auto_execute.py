"""ROB-402 — watch auto_execute_mock service.

Records an intent (audit) and, when all gates pass, places a kis_mock order.
The executor is hard-pinned is_mock=True; the live-block guard rejects explicit
live/non-mock accounts before any insert. Default off via gate flag.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models.review import WatchOrderIntentLedger
from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)

logger = logging.getLogger(__name__)


def _to_decimal(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _PlaceOutcome:
    executed: bool
    reason: str | None = None
    detail: str | None = None


def _normalize_place_result(result: Any) -> _PlaceOutcome:
    """Interpret the order function's normalized result truthfully (ROB-843).

    A non-dict result or an explicit ``success=False`` is a failure — never a
    silent executed=True. The stable reason/detail is preserved from the
    normalized native contract so the watch intent records why it failed.
    """
    if not isinstance(result, dict):
        return _PlaceOutcome(False, "malformed_result", str(result)[:200])
    if result.get("success"):
        return _PlaceOutcome(True)
    reason = result.get("reason") or result.get("status") or "order_failed"
    detail = (
        result.get("detail") or result.get("response_message") or result.get("message")
    )
    return _PlaceOutcome(False, str(reason), str(detail) if detail else None)


async def _default_place_order_fn(**kwargs):
    # Lazy import to avoid heavy import at module load.
    from app.mcp_server.tooling.order_execution import _place_order_impl

    return await _place_order_impl(**kwargs)


async def maybe_auto_execute(
    db,
    *,
    alert,
    correlation_id: str,
    kst_date: str,
    place_order_fn: Callable[..., Any] = _default_place_order_fn,
) -> dict[str, Any]:
    """Evaluate gates and (if all pass) place a kis_mock order for the alert."""
    if alert.action_mode != "auto_execute_mock":
        return {"executed": False, "skipped": "not_auto_execute_mock"}

    max_action: dict = alert.max_action or {}
    account_mode = max_action.get("account_mode") or "kis_mock"

    # 1) live-block guard (hard reject before any insert).
    try:
        assert_auto_execute_account_allowed("auto_execute_mock", account_mode)
    except AutoExecuteLiveBlocked:
        logger.warning(
            "auto_execute_mock blocked for live account on alert %s", alert.alert_uuid
        )
        return {"executed": False, "blocked_by": "live_account"}
    except AutoExecuteUnsupported:
        logger.warning(
            "auto_execute_mock unsupported account on alert %s", alert.alert_uuid
        )
        return {"executed": False, "blocked_by": "unsupported_account"}

    # 2) precondition checks (account is kis_mock from here on).
    reasons: list[str] = []
    if not settings.WATCH_AUTO_EXECUTE_MOCK_ENABLED:
        reasons.append("auto_execute_globally_disabled")
    side = max_action.get("side")
    quantity = _to_decimal(max_action.get("quantity"))
    limit_price = _to_decimal(max_action.get("limit_price"))
    if side not in ("buy", "sell"):
        reasons.append("missing_or_invalid_side")
    if quantity is None or quantity <= 0:
        reasons.append("missing_quantity")
    if limit_price is None or limit_price <= 0:
        reasons.append("missing_limit_price")

    allowed = not reasons
    lifecycle = "previewed" if allowed else "failed"
    preview_line = {
        "symbol": alert.symbol,
        "side": side,
        "quantity": str(quantity) if quantity is not None else None,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "account_mode": "kis_mock",
        "action_mode": "auto_execute_mock",
    }

    # 3) write intent row (ON CONFLICT correlation_id → idempotent skip).
    stmt = (
        pg_insert(WatchOrderIntentLedger)
        .values(
            correlation_id=correlation_id,
            idempotency_key=f"intent:{alert.alert_uuid}:{kst_date}:{alert.threshold_key}",
            market=alert.market,
            target_kind=alert.target_kind,
            symbol=alert.symbol,
            condition_type=alert.operator,
            threshold=_to_decimal(alert.threshold),
            threshold_key=alert.threshold_key,
            action="auto_execute_mock",
            side=side if side in ("buy", "sell") else "buy",
            account_mode="kis_mock",
            execution_source="watch",
            lifecycle_state=lifecycle,
            quantity=quantity,
            limit_price=limit_price,
            execution_allowed=allowed,
            approval_required=False,
            blocking_reasons=reasons,
            blocked_by=(reasons[0] if reasons else None),
            preview_line=preview_line,
            kst_date=kst_date,
        )
        .on_conflict_do_nothing(constraint="uq_watch_intent_correlation_id")
        .returning(WatchOrderIntentLedger.id)
    )
    result = await db.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await db.commit()

    if inserted_id is None:
        return {"executed": False, "skipped": "duplicate"}
    if not allowed:
        return {"executed": False, "blocking_reasons": reasons}

    # 4) place the kis_mock order (executor hard-pinned is_mock=True). A raised
    # exception is a failure too — never leave the intent 'previewed' (ROB-843).
    try:
        place_result: Any = await place_order_fn(
            symbol=alert.symbol,
            side=side,
            order_type="limit",
            quantity=float(quantity),
            price=float(limit_price),
            dry_run=False,
            reason="watch auto_execute_mock",
            is_mock=True,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a truthful failed outcome
        place_result = {
            "success": False,
            "reason": "order_exception",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }

    # 5) validate + persist the broker outcome truthfully (ROB-843). The result
    # is never discarded: a failure flips the intent row to 'failed' with the
    # stable reason/detail preserved, and returns executed=False.
    outcome = _normalize_place_result(place_result)
    if not outcome.executed:
        logger.warning(
            "auto_execute_mock order failed alert=%s reason=%s",
            alert.alert_uuid,
            outcome.reason,
        )
        await _mark_intent_failed(
            db,
            correlation_id=correlation_id,
            reason=outcome.reason,
            detail=outcome.detail,
            preview_line=preview_line,
        )
        return {
            "executed": False,
            "reason": outcome.reason,
            "detail": outcome.detail,
            "correlation_id": correlation_id,
        }

    return {"executed": True, "correlation_id": correlation_id}


async def _mark_intent_failed(
    db,
    *,
    correlation_id: str,
    reason: str | None,
    detail: str | None,
    preview_line: dict[str, Any],
) -> None:
    """Flip the previewed intent row to 'failed', preserving reason/detail.

    Reuses existing columns only (no schema migration): ``blocked_by`` /
    ``blocking_reasons`` carry the reason and ``preview_line.failure_detail``
    carries the redacted broker detail.
    """
    reason = reason or "order_failed"
    failed_preview = {**preview_line, "failure_detail": detail}
    await db.execute(
        update(WatchOrderIntentLedger)
        .where(WatchOrderIntentLedger.correlation_id == correlation_id)
        .values(
            lifecycle_state="failed",
            execution_allowed=False,
            blocked_by=reason,
            blocking_reasons=[reason],
            preview_line=failed_preview,
        )
    )
    await db.commit()
