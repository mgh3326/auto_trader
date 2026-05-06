"""ROB-118 — OrderPreviewSessionService.

This is the ONLY allowed write path for order_preview_session/leg/execution_request.
All callers must go through this service. Direct SQL writes are forbidden.
"""

from __future__ import annotations

import secrets
import uuid
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order_preview_session import (
    OrderExecutionRequest,
    OrderPreviewLeg,
    OrderPreviewSession,
)
from app.schemas.order_preview_session import (
    CreatePreviewRequest,
    PreviewSessionOut,
    SubmitPreviewRequest,
)


class DryRunRunner(Protocol):
    async def run(self, *, payload: dict[str, Any]) -> dict[str, Any]: ...


class PreviewSessionNotFoundError(Exception):
    pass


class PreviewSchemaMismatchError(Exception):
    """Raised when broker tool schema mismatch is detected — fail-closed."""


class PreviewNotApprovedError(Exception):
    pass


class OrderPreviewSessionService:
    def __init__(self, *, db: AsyncSession, dry_run: DryRunRunner) -> None:
        self._db = db
        self._dry_run = dry_run

    async def create_preview(
        self, *, user_id: int, request: CreatePreviewRequest
    ) -> PreviewSessionOut:
        session = OrderPreviewSession(
            preview_uuid=str(uuid.uuid4()),
            user_id=user_id,
            source_kind=request.source_kind,
            source_ref=request.source_ref,
            research_session_id=request.research_session_id,
            symbol=request.symbol,
            market=request.market,
            venue=request.venue,
            side=request.side,
            status="created",
            approval_token=secrets.token_urlsafe(24),
        )
        for leg_in in request.legs:
            session.legs.append(
                OrderPreviewLeg(
                    leg_index=leg_in.leg_index,
                    quantity=leg_in.quantity,
                    price=leg_in.price,
                    order_type=leg_in.order_type,
                )
            )
        self._db.add(session)
        await self._db.flush()

        await self._run_dry_run_inplace(session)

        await self._db.commit()
        await self._db.refresh(session, attribute_names=["legs", "executions"])
        return PreviewSessionOut.model_validate(session)

    async def refresh_preview(
        self, *, user_id: int, preview_uuid: str
    ) -> PreviewSessionOut:
        session = await self._load_owned(user_id=user_id, preview_uuid=preview_uuid)
        await self._run_dry_run_inplace(session)
        await self._db.commit()
        await self._db.refresh(session, attribute_names=["legs", "executions"])
        return PreviewSessionOut.model_validate(session)

    async def submit_preview(
        self,
        *,
        user_id: int,
        preview_uuid: str,
        request: SubmitPreviewRequest,
        broker_submit,  # async (leg) -> {"order_id": str, ...}
    ) -> PreviewSessionOut:
        session = await self._load_owned(user_id=user_id, preview_uuid=preview_uuid)

        if session.status != "preview_passed":
            raise PreviewNotApprovedError(
                f"submit blocked: status={session.status}"
            )
        if not session.approval_token or not secrets.compare_digest(
            session.approval_token, request.approval_token
        ):
            raise PreviewNotApprovedError("approval_token mismatch")

        from datetime import datetime, timezone

        session.approved_at = datetime.now(timezone.utc)

        any_failure = False
        for leg in session.legs:
            try:
                result = await broker_submit(leg=leg, session=session)
            except Exception as exc:  # noqa: BLE001
                any_failure = True
                self._db.add(
                    OrderExecutionRequest(
                        session_id=session.id,
                        leg_id=leg.id,
                        broker_order_id=None,
                        status="failed",
                        error_payload={"message": str(exc)},
                    )
                )
                continue
            self._db.add(
                OrderExecutionRequest(
                    session_id=session.id,
                    leg_id=leg.id,
                    broker_order_id=str(result.get("order_id") or "") or None,
                    status="submitted",
                    error_payload=None,
                )
            )

        session.status = "submit_failed" if any_failure else "submitted"
        session.submitted_at = datetime.now(timezone.utc)
        await self._db.commit()
        await self._db.refresh(session, attribute_names=["legs", "executions"])
        return PreviewSessionOut.model_validate(session)

    async def get(self, *, user_id: int, preview_uuid: str) -> PreviewSessionOut:
        session = await self._load_owned(user_id=user_id, preview_uuid=preview_uuid)
        return PreviewSessionOut.model_validate(session)

    async def _run_dry_run_inplace(self, session: OrderPreviewSession) -> None:
        payload = {
            "symbol": session.symbol,
            "market": session.market,
            "venue": session.venue,
            "side": session.side,
            "legs": [
                {
                    "leg_index": leg.leg_index,
                    "quantity": str(leg.quantity),
                    "price": str(leg.price) if leg.price is not None else None,
                    "order_type": leg.order_type,
                }
                for leg in session.legs
            ],
        }
        try:
            result = await self._dry_run.run(payload=payload)
        except PreviewSchemaMismatchError as exc:
            session.status = "preview_failed"
            session.dry_run_error = {"kind": "schema_mismatch", "message": str(exc)}
            for leg in session.legs:
                leg.dry_run_status = "failed"
                leg.dry_run_error = {"kind": "schema_mismatch"}
            return
        except Exception as exc:  # noqa: BLE001 — fail-closed on any preview error
            session.status = "preview_failed"
            session.dry_run_error = {"kind": "exception", "message": str(exc)}
            for leg in session.legs:
                leg.dry_run_status = "failed"
                leg.dry_run_error = {"message": str(exc)}
            return

        if not result.get("ok"):
            session.status = "preview_failed"
            session.dry_run_error = result
            for leg in session.legs:
                leg.dry_run_status = "failed"
                leg.dry_run_error = result
            return

        session.dry_run_payload = result
        result_legs = {l["leg_index"]: l for l in result.get("legs", [])}
        for leg in session.legs:
            r = result_legs.get(leg.leg_index)
            if r is None:
                # Schema mismatch — fail-closed
                session.status = "preview_failed"
                session.dry_run_error = {
                    "kind": "schema_mismatch",
                    "message": f"missing leg_index={leg.leg_index} in dry_run result",
                }
                leg.dry_run_status = "failed"
                leg.dry_run_error = {"kind": "schema_mismatch"}
                return
            leg.estimated_value = _to_decimal(r.get("estimated_value"))
            leg.estimated_fee = _to_decimal(r.get("estimated_fee"))
            leg.expected_pnl = _to_decimal(r.get("expected_pnl"))
            leg.dry_run_status = "passed"
            leg.dry_run_error = None
        session.status = "preview_passed"

    async def _load_owned(
        self, *, user_id: int, preview_uuid: str
    ) -> OrderPreviewSession:
        stmt = (
            select(OrderPreviewSession)
            .where(
                OrderPreviewSession.preview_uuid == preview_uuid,
                OrderPreviewSession.user_id == user_id,
            )
            .options(
                selectinload(OrderPreviewSession.legs),
                selectinload(OrderPreviewSession.executions).selectinload(OrderExecutionRequest.leg),
            )
        )
        result = await self._db.execute(stmt)
        session = result.scalar_one_or_none()
        if session is None:
            raise PreviewSessionNotFoundError(preview_uuid)
        return session


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
