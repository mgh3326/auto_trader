"""Direct watch-alert creation independent of investment report activation."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.investment_reports import InvestmentWatchAlert
from app.schemas.investment_reports import CreateInvestmentWatchRequest
from app.services.investment_reports.idempotency import direct_watch_key
from app.services.investment_reports.repository import InvestmentReportsRepository

_DIRECT_WATCH_NAMESPACE = uuid.UUID("7d85169b-7e5d-4d53-87eb-1bb7ba8ecf60")


class DirectWatchCreateService:
    """Create active watch alerts without report/item source rows."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def create(
        self, request: CreateInvestmentWatchRequest
    ) -> tuple[InvestmentWatchAlert, bool]:
        if request.valid_until <= now_kst():
            raise ValueError("valid_until must be in the future")

        condition = request.watch_condition.model_dump(mode="json")
        if condition.get("action_mode") == "auto_execute_mock":
            raise ValueError("investment_watch_create does not allow auto_execute_mock")

        normalized_symbol = _normalize_symbol(request.symbol, request.market)
        key = request.idempotency_key or direct_watch_key(
            created_by=request.created_by,
            market=request.market,
            symbol=normalized_symbol,
            intent=request.intent,
            valid_until=request.valid_until.isoformat(),
            watch_condition=condition,
        )

        existing = await self._repo.get_alert_by_idempotency_key(key)
        if existing is not None:
            _assert_same_identity(existing, request, normalized_symbol)
            return existing, True

        fields = _alert_fields(request, normalized_symbol, condition, key)
        alert = await self._repo.insert_alert(**fields)
        await self._session.flush()
        return alert, False


def _normalize_symbol(symbol: str, market: str) -> str:
    stripped = symbol.strip()
    if market in {"us", "crypto"}:
        return stripped.upper()
    return stripped


def _alert_fields(
    request: CreateInvestmentWatchRequest,
    symbol: str,
    condition: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    clauses = list(condition.get("conditions") or [])
    primary = clauses[0]
    operator = primary["op"]
    if operator == "between":
        threshold = _to_decimal(primary.get("low"))
        threshold_high = _to_decimal(primary.get("high"))
    else:
        threshold = _to_decimal(primary.get("threshold"))
        threshold_high = None

    metadata = dict(request.metadata)
    metadata.update(
        {
            "created_by": request.created_by,
            "source_tool": "investment_watch_create",
        }
    )

    return {
        "idempotency_key": idempotency_key,
        "source_report_uuid": _source_uuid(idempotency_key, "report"),
        "source_item_uuid": _source_uuid(idempotency_key, "item"),
        "market": request.market,
        "target_kind": condition.get("target_kind", "asset"),
        "symbol": symbol,
        "metric": primary["metric"],
        "operator": operator,
        "threshold": threshold,
        "threshold_high": threshold_high,
        "threshold_key": condition["threshold_key"],
        "conditions": clauses,
        "combine": condition.get("combine", "and"),
        "intent": request.intent,
        "action_mode": condition.get("action_mode", "notify_only"),
        "rationale": request.rationale,
        "trigger_checklist": list(request.trigger_checklist),
        "max_action": dict(request.max_action),
        "valid_until": request.valid_until,
        "status": "active",
        "alert_metadata": metadata,
    }


def _assert_same_identity(
    existing: InvestmentWatchAlert,
    request: CreateInvestmentWatchRequest,
    symbol: str,
) -> None:
    if (
        existing.market != request.market
        or existing.symbol != symbol
        or existing.intent != request.intent
    ):
        raise ValueError(
            f"idempotency_key {existing.idempotency_key!r} already used for "
            "a different watch identity"
        )


def _source_uuid(idempotency_key: str, slot: str) -> uuid.UUID:
    return uuid.uuid5(_DIRECT_WATCH_NAMESPACE, f"{slot}:{idempotency_key}")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("threshold is required in watch_condition")
    return Decimal(str(value))
