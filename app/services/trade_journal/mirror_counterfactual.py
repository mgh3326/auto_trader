from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.models.review import KISMockOrderLedger

MirrorSourceBucket = Literal[
    "place_original",
    "watch_trigger",
    "deferred_min_rung",
]

_PRICE_RE = re.compile(
    r"(?:limit_price|price)\s*[:=]\s*([0-9][0-9,]*(?:\.[0-9]+)?)|"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*원"
)

_KR_EQUITY_SYMBOL_RE = re.compile(r"^\d{6}$")
_NON_KR_EQUITY_REASON = "non_kr_equity_out_of_mirror_scope"

PlaceOrderCallable = Callable[..., Awaitable[dict[str, Any]]]


def _mirror_scope_skip_reason(
    report_market: str,
    item: InvestmentReportItem,
) -> str | None:
    symbol = str(item.symbol or "").strip()
    if report_market != "kr":
        return _NON_KR_EQUITY_REASON
    if item.target_kind != "asset":
        return _NON_KR_EQUITY_REASON
    if _KR_EQUITY_SYMBOL_RE.fullmatch(symbol) is None:
        return _NON_KR_EQUITY_REASON
    return None


@dataclass(frozen=True)
class MirrorOrderPlan:
    report_uuid: UUID
    item_uuid: UUID
    source_bucket: MirrorSourceBucket
    correlation_id: str
    symbol: str
    side: str
    quantity: Decimal | None
    amount: Decimal | None
    price: Decimal
    target_price: Decimal | None
    stop_loss: Decimal | None
    min_hold_days: int | None
    reason: str
    thesis: str | None
    strategy: str
    notes: str


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out > 0 else None


def _price_from_trigger_checklist(checklist: list[Any]) -> Decimal | None:
    if not checklist:
        return None
    for item in checklist:
        text_val = str(item)
        match = _PRICE_RE.search(text_val)
        if match:
            val = match.group(1) or match.group(2)
            if val:
                dec = _decimal(val)
                if dec is not None:
                    return dec
    return None


def _side_from_item(item: InvestmentReportItem) -> str | None:
    if item.side in ("buy", "sell"):
        return item.side
    max_action = item.max_action or {}
    side = max_action.get("side")
    if side in ("buy", "sell"):
        return side
    intent = str(item.intent or "").lower()
    if "buy" in intent:
        return "buy"
    if "sell" in intent:
        return "sell"
    return None


def _price_from_item(item: InvestmentReportItem) -> Decimal | None:
    max_action = item.max_action or {}

    limit_price = max_action.get("limit_price")
    if limit_price not in (None, ""):
        val = _decimal(limit_price)
        if val is not None:
            return val

    limit_price_hint = max_action.get("limit_price_hint")
    if limit_price_hint not in (None, ""):
        val = _decimal(limit_price_hint)
        if val is not None:
            return val

    watch_cond = item.watch_condition or {}
    threshold = watch_cond.get("threshold")
    if threshold not in (None, ""):
        val = _decimal(threshold)
        if val is not None:
            return val

    chk_val = _price_from_trigger_checklist(item.trigger_checklist)
    if chk_val is not None:
        return chk_val

    ev = item.evidence_snapshot or {}
    trade_setup = ev.get("trade_setup") or {}
    if isinstance(trade_setup, dict):
        entry = trade_setup.get("entry")
        if entry not in (None, ""):
            val = _decimal(entry)
            if val is not None:
                return val

    price = ev.get("price")
    if price not in (None, ""):
        val = _decimal(price)
        if val is not None:
            return val

    current_price = ev.get("current_price")
    if current_price not in (None, ""):
        val = _decimal(current_price)
        if val is not None:
            return val

    return None


def _quantity_from_item(
    item: InvestmentReportItem,
    min_rung_quantity: Decimal,
    source_bucket: MirrorSourceBucket,
) -> Decimal | None:
    if source_bucket == "deferred_min_rung":
        return min_rung_quantity

    max_action = item.max_action or {}
    qty = max_action.get("quantity")
    if qty not in (None, ""):
        return _decimal(qty)

    return None


def _amount_from_item(
    item: InvestmentReportItem,
    source_bucket: MirrorSourceBucket,
) -> Decimal | None:
    if source_bucket == "deferred_min_rung":
        return None

    max_action = item.max_action or {}
    side = _side_from_item(item)
    if side != "buy":
        return None

    notional = max_action.get("notional")
    if notional not in (None, ""):
        return _decimal(notional)

    amount_krw = max_action.get("amount_krw")
    if amount_krw not in (None, ""):
        return _decimal(amount_krw)

    return None


def _target_from_item(item: InvestmentReportItem) -> Decimal | None:
    ev = item.evidence_snapshot or {}
    trade_setup = ev.get("trade_setup") or {}
    if isinstance(trade_setup, dict):
        target = trade_setup.get("target")
        if target not in (None, ""):
            val = _decimal(target)
            if val is not None:
                return val
    max_action = item.max_action or {}
    target_price = max_action.get("target_price")
    if target_price not in (None, ""):
        val = _decimal(target_price)
        if val is not None:
            return val
    return None


def _stop_from_item(item: InvestmentReportItem) -> Decimal | None:
    ev = item.evidence_snapshot or {}
    trade_setup = ev.get("trade_setup") or {}
    if isinstance(trade_setup, dict):
        stop = trade_setup.get("stop")
        if stop not in (None, ""):
            val = _decimal(stop)
            if val is not None:
                return val
    max_action = item.max_action or {}
    stop_loss = max_action.get("stop_loss")
    if stop_loss not in (None, ""):
        val = _decimal(stop_loss)
        if val is not None:
            return val
    return None


def _min_hold_days_from_item(item: InvestmentReportItem) -> int | None:
    max_action = item.max_action or {}
    mhd = max_action.get("min_hold_days")
    if mhd not in (None, ""):
        try:
            return int(float(str(mhd)))
        except (ValueError, TypeError):
            return None
    return None


def _watch_metric(value: Any) -> str | None:
    token = str(value or "").strip().lower()
    return token or None


def _watch_price_skip_reason(item: InvestmentReportItem) -> str | None:
    if item.item_kind != "watch":
        return None
    watch_cond = item.watch_condition or {}
    threshold = watch_cond.get("threshold")
    if threshold in (None, ""):
        return None
    metric = _watch_metric(watch_cond.get("metric") or "price")
    if metric != "price":
        return "unsupported_watch_metric_for_limit_price"
    return None


def _watch_notes(item: InvestmentReportItem) -> list[str]:
    if item.item_kind != "watch":
        return []
    watch_cond = item.watch_condition or {}
    metric = _watch_metric(watch_cond.get("metric") or "price")
    operator = str(watch_cond.get("operator") or "").strip().lower()
    notes = [f"watch_metric={metric or 'unknown'}"]
    if operator:
        notes.append(f"watch_operator={operator}")
    if metric == "price" and operator not in {"below", "at_or_below", "lte", "<="}:
        notes.append("watch_approximation=limit_at_threshold")
    return notes


def _plan_for_item(
    report_market: str,
    report_uuid: UUID,
    item: InvestmentReportItem,
    min_rung_quantity: Decimal,
) -> tuple[MirrorOrderPlan | None, str | None]:
    if not item.symbol:
        return None, "missing_symbol"

    symbol = item.symbol.strip()
    scope_skip_reason = _mirror_scope_skip_reason(report_market, item)
    if scope_skip_reason is not None:
        return None, scope_skip_reason

    source_bucket: MirrorSourceBucket | None = None
    side = _side_from_item(item)

    if item.decision_bucket == "deferred_no_action":
        source_bucket = "deferred_min_rung"
        if side is None:
            side = "buy"
    elif item.item_kind == "action" and side in ("buy", "sell"):
        source_bucket = "place_original"
    elif item.item_kind == "watch":
        source_bucket = "watch_trigger"

    if source_bucket is None:
        return None, "unsupported_item_kind_or_bucket"

    if not side:
        return None, "missing_side"

    watch_skip = _watch_price_skip_reason(item)
    if watch_skip is not None:
        return None, watch_skip

    price = _price_from_item(item)
    if price is None:
        return None, "missing_limit_price"

    qty = _quantity_from_item(item, min_rung_quantity, source_bucket)
    amount = _amount_from_item(item, source_bucket)
    if qty is None and amount is None:
        return None, "missing_quantity_or_amount"

    correlation_id = f"mirror:{item.item_uuid}"

    plan = MirrorOrderPlan(
        report_uuid=report_uuid,
        item_uuid=item.item_uuid,
        source_bucket=source_bucket,
        correlation_id=correlation_id,
        symbol=symbol,
        side=side,
        quantity=qty,
        amount=amount,
        price=price,
        target_price=_target_from_item(item),
        stop_loss=_stop_from_item(item),
        min_hold_days=_min_hold_days_from_item(item),
        reason=f"ROB-734 mirror counterfactual: {source_bucket}",
        thesis=item.rationale or "counterfactual mirror",
        strategy="mirror_counterfactual",
        notes=";".join([f"source_bucket={source_bucket}", *_watch_notes(item)]),
    )
    return plan, None


async def build_mirror_order_plans(
    db: AsyncSession,
    *,
    report_uuid: UUID,
    min_rung_quantity: Decimal = Decimal("1"),
) -> dict[str, Any]:
    report = await db.scalar(
        select(InvestmentReport).where(InvestmentReport.report_uuid == report_uuid)
    )
    if report is None:
        raise ValueError(f"report not found: {report_uuid}")
    rows = (
        (
            await db.execute(
                select(InvestmentReportItem)
                .where(InvestmentReportItem.report_id == report.id)
                .order_by(
                    InvestmentReportItem.created_at.asc(), InvestmentReportItem.id.asc()
                )
            )
        )
        .scalars()
        .all()
    )
    plans: list[MirrorOrderPlan] = []
    skipped: list[dict[str, str]] = []
    for item in rows:
        plan, reason = _plan_for_item(
            report_market=report.market,
            report_uuid=report.report_uuid,
            item=item,
            min_rung_quantity=min_rung_quantity,
        )
        if plan is None:
            skipped.append(
                {"item_uuid": str(item.item_uuid), "reason": reason or "unknown"}
            )
        else:
            plans.append(plan)
    return {
        "report_uuid": str(report.report_uuid),
        "plans": plans,
        "skipped": skipped,
        "count": len(plans),
    }


async def _default_place_order(**kwargs: Any) -> dict[str, Any]:
    from app.mcp_server.tooling import order_execution

    return await order_execution._place_order_impl(**kwargs)


async def _existing_mirror_item_uuids(
    db: AsyncSession, item_uuids: list[UUID]
) -> set[UUID]:
    if not item_uuids:
        return set()
    rows = (
        (
            await db.execute(
                select(KISMockOrderLedger.report_item_uuid)
                .where(KISMockOrderLedger.report_item_uuid.in_(item_uuids))
                .where(KISMockOrderLedger.mirror_cohort == "mock_counterfactual")
            )
        )
        .scalars()
        .all()
    )
    return {row for row in rows if row is not None}


async def _stamp_mirror_ledger(
    db: AsyncSession, *, ledger_id: int, plan: MirrorOrderPlan
) -> None:
    row = await db.get(KISMockOrderLedger, ledger_id)
    if row is None:
        raise ValueError(f"kis_mock ledger row not found: {ledger_id}")
    row.report_item_uuid = plan.item_uuid
    row.mirror_cohort = "mock_counterfactual"
    row.mirror_source_bucket = plan.source_bucket
    await db.flush()


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _plan_summary(plan: MirrorOrderPlan) -> dict[str, Any]:
    return {
        "item_uuid": str(plan.item_uuid),
        "source_bucket": plan.source_bucket,
        "correlation_id": plan.correlation_id,
        "symbol": plan.symbol,
        "side": plan.side,
        "quantity": _decimal_str(plan.quantity),
        "amount": _decimal_str(plan.amount),
        "price": _decimal_str(plan.price),
        "target_price": _decimal_str(plan.target_price),
        "stop_loss": _decimal_str(plan.stop_loss),
        "min_hold_days": plan.min_hold_days,
        "notes": plan.notes,
    }


def _execution_status(
    *,
    planned_count: int,
    dry_run_count: int,
    submitted_count: int,
    skipped_count: int,
    failed_count: int,
) -> str:
    if failed_count == 0:
        return "ok"
    non_failed = dry_run_count + submitted_count + skipped_count
    if planned_count > 0 and non_failed == 0:
        return "failed"
    return "partial_failure"


async def execute_mirror_order_plans(
    db: AsyncSession,
    *,
    plans: list[MirrorOrderPlan],
    dry_run: bool = True,
    place_order: PlaceOrderCallable | None = None,
) -> dict[str, Any]:
    if place_order is None:
        place_order = _default_place_order

    results = []
    submitted_count = 0
    dry_run_count = 0
    skipped_count = 0
    failed_count = 0

    item_uuids = [p.item_uuid for p in plans]
    existing = await _existing_mirror_item_uuids(db, item_uuids)

    for plan in plans:
        if plan.item_uuid in existing:
            results.append(
                {
                    "item_uuid": str(plan.item_uuid),
                    "symbol": plan.symbol,
                    "success": False,
                    "reason": "already_mirrored",
                    "plan": _plan_summary(plan),
                }
            )
            skipped_count += 1
            continue

        try:
            result = await place_order(
                symbol=plan.symbol,
                side=plan.side,
                order_type="limit",
                quantity=float(plan.quantity) if plan.quantity is not None else None,
                amount=float(plan.amount) if plan.amount is not None else None,
                price=float(plan.price),
                dry_run=dry_run,
                reason=plan.reason,
                thesis=plan.thesis,
                strategy=plan.strategy,
                target_price=float(plan.target_price)
                if plan.target_price is not None
                else None,
                stop_loss=float(plan.stop_loss) if plan.stop_loss is not None else None,
                min_hold_days=plan.min_hold_days,
                notes=plan.notes,
                is_mock=True,
                correlation_id=plan.correlation_id,
                report_item_uuid=str(plan.item_uuid),
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket=plan.source_bucket,
            )

            success = result.get("success", False)
            if success:
                if dry_run:
                    dry_run_count += 1
                    results.append(
                        {
                            "item_uuid": str(plan.item_uuid),
                            "symbol": plan.symbol,
                            "success": True,
                            "dry_run": True,
                            "approval_hash": result.get("approval_hash"),
                            "plan": _plan_summary(plan),
                        }
                    )
                else:
                    ledger_id = result.get("ledger_id")
                    if ledger_id is not None:
                        row = await db.get(KISMockOrderLedger, ledger_id)
                        if row and (
                            row.report_item_uuid != plan.item_uuid
                            or row.mirror_cohort != "mock_counterfactual"
                            or row.mirror_source_bucket != plan.source_bucket
                        ):
                            await _stamp_mirror_ledger(
                                db, ledger_id=ledger_id, plan=plan
                            )
                        submitted_count += 1
                        results.append(
                            {
                                "item_uuid": str(plan.item_uuid),
                                "symbol": plan.symbol,
                                "success": True,
                                "ledger_id": ledger_id,
                                "plan": _plan_summary(plan),
                            }
                        )
                    else:
                        failed_count += 1
                        results.append(
                            {
                                "item_uuid": str(plan.item_uuid),
                                "symbol": plan.symbol,
                                "success": False,
                                "error": "missing_ledger_id",
                                "plan": _plan_summary(plan),
                            }
                        )
            else:
                failed_count += 1
                results.append(
                    {
                        "item_uuid": str(plan.item_uuid),
                        "symbol": plan.symbol,
                        "success": False,
                        "error": result.get("error") or "place_order_failed",
                        "plan": _plan_summary(plan),
                    }
                )
        except Exception as exc:
            failed_count += 1
            results.append(
                {
                    "item_uuid": str(plan.item_uuid),
                    "symbol": plan.symbol,
                    "success": False,
                    "error": str(exc),
                    "plan": _plan_summary(plan),
                }
            )

    status = _execution_status(
        planned_count=len(plans),
        dry_run_count=dry_run_count,
        submitted_count=submitted_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
    )
    return {
        "success": failed_count == 0,
        "status": status,
        "dry_run": dry_run,
        "cohort": "mock_counterfactual",
        "planned_count": len(plans),
        "submitted_count": submitted_count,
        "dry_run_count": dry_run_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "results": results,
        "caveats": [
            "KIS mock fills do not model queue priority, liquidity, slippage, or market impact; mock performance is upward biased."
        ],
    }


async def execute_mirror_for_report(
    db: AsyncSession,
    *,
    report_uuid: UUID,
    dry_run: bool = True,
    min_rung_quantity: Decimal = Decimal("1"),
    place_order: PlaceOrderCallable | None = None,
) -> dict[str, Any]:
    plans_res = await build_mirror_order_plans(
        db, report_uuid=report_uuid, min_rung_quantity=min_rung_quantity
    )
    plans = plans_res["plans"]
    exec_res = await execute_mirror_order_plans(
        db, plans=plans, dry_run=dry_run, place_order=place_order
    )
    exec_res["skipped_plans"] = plans_res["skipped"]
    exec_res["plan_skipped_count"] = len(plans_res["skipped"])
    return exec_res
