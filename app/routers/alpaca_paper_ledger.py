"""Read-only Alpaca Paper order ledger router (ROB-84/ROB-90).

GET paths only. No POST/PATCH/DELETE. No broker mutation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.alpaca_paper_ledger import (
    AlpacaPaperOrderLedgerCorrelationResponse,
    AlpacaPaperOrderLedgerListResponse,
    AlpacaPaperOrderLedgerRead,
)
from app.schemas.alpaca_paper_roundtrip_report import (
    AlpacaPaperRoundtripReport,
    AlpacaPaperRoundtripReportListResponse,
)
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_roundtrip_report_service import (
    AlpacaPaperRoundtripReportService,
)

router = APIRouter(prefix="/trading", tags=["alpaca-paper-ledger"])


@router.get(
    "/api/alpaca-paper/ledger/recent",
    response_model=AlpacaPaperOrderLedgerListResponse,
)
async def list_recent_ledger_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    limit: int = 50,
    lifecycle_state: str | None = None,
) -> AlpacaPaperOrderLedgerListResponse:
    svc = AlpacaPaperLedgerService(db)
    rows = await svc.list_recent(limit=limit, lifecycle_state=lifecycle_state)
    return AlpacaPaperOrderLedgerListResponse(
        count=len(rows),
        items=[AlpacaPaperOrderLedgerRead.model_validate(r) for r in rows],
    )


@router.get(
    "/api/alpaca-paper/ledger/by-correlation-id/{lifecycle_correlation_id}",
    response_model=AlpacaPaperOrderLedgerCorrelationResponse,
)
async def get_ledger_by_correlation_id(
    lifecycle_correlation_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> AlpacaPaperOrderLedgerCorrelationResponse:
    """Return all ledger rows sharing a lifecycle_correlation_id (buy/sell roundtrip)."""
    svc = AlpacaPaperLedgerService(db)
    rows = await svc.list_by_correlation_id(lifecycle_correlation_id)
    return AlpacaPaperOrderLedgerCorrelationResponse(
        lifecycle_correlation_id=lifecycle_correlation_id,
        count=len(rows),
        items=[AlpacaPaperOrderLedgerRead.model_validate(r) for r in rows],
    )


@router.get(
    "/api/alpaca-paper/ledger/by-client-order-id/{client_order_id}",
    response_model=AlpacaPaperOrderLedgerRead,
)
async def get_ledger_by_client_order_id(
    client_order_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> AlpacaPaperOrderLedgerRead:
    svc = AlpacaPaperLedgerService(db)
    row = await svc.get_by_client_order_id(client_order_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ledger entry not found for client_order_id={client_order_id!r}",
        )
    return AlpacaPaperOrderLedgerRead.model_validate(row)


@router.get(
    "/api/alpaca-paper/roundtrip-report/by-correlation-id/{lifecycle_correlation_id}",
    response_model=AlpacaPaperRoundtripReport,
)
async def get_roundtrip_report_by_correlation_id(
    lifecycle_correlation_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    include_ledger_rows: bool = True,
    stale_after_minutes: int = 30,
) -> AlpacaPaperRoundtripReport:
    svc = AlpacaPaperRoundtripReportService(db)
    report = await svc.build_report(
        lifecycle_correlation_id=lifecycle_correlation_id,
        include_ledger_rows=include_ledger_rows,
        stale_after_minutes=stale_after_minutes,
    )
    if report.status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Roundtrip report not found for "
                f"lifecycle_correlation_id={lifecycle_correlation_id!r}"
            ),
        )
    return report


@router.get(
    "/api/alpaca-paper/roundtrip-report/by-client-order-id/{client_order_id}",
    response_model=AlpacaPaperRoundtripReport,
)
async def get_roundtrip_report_by_client_order_id(
    client_order_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    include_ledger_rows: bool = True,
    stale_after_minutes: int = 30,
) -> AlpacaPaperRoundtripReport:
    svc = AlpacaPaperRoundtripReportService(db)
    report = await svc.build_report(
        client_order_id=client_order_id,
        include_ledger_rows=include_ledger_rows,
        stale_after_minutes=stale_after_minutes,
    )
    if report.status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Roundtrip report not found for client_order_id={client_order_id!r}",
        )
    return report


@router.get(
    "/api/alpaca-paper/roundtrip-report/by-candidate-uuid/{candidate_uuid}",
    response_model=AlpacaPaperRoundtripReportListResponse,
)
async def get_roundtrip_reports_by_candidate_uuid(
    candidate_uuid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    include_ledger_rows: bool = True,
    stale_after_minutes: int = 30,
) -> AlpacaPaperRoundtripReportListResponse:
    svc = AlpacaPaperRoundtripReportService(db)
    response = await svc.build_reports_for_candidate_uuid(
        candidate_uuid,
        include_ledger_rows=include_ledger_rows,
        stale_after_minutes=stale_after_minutes,
    )
    if response.count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Roundtrip reports not found for candidate_uuid={candidate_uuid}",
        )
    return response


@router.get(
    "/api/alpaca-paper/roundtrip-report/by-briefing-artifact-run-uuid/{briefing_artifact_run_uuid}",
    response_model=AlpacaPaperRoundtripReportListResponse,
)
async def get_roundtrip_reports_by_briefing_artifact_run_uuid(
    briefing_artifact_run_uuid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    include_ledger_rows: bool = True,
    stale_after_minutes: int = 30,
) -> AlpacaPaperRoundtripReportListResponse:
    svc = AlpacaPaperRoundtripReportService(db)
    response = await svc.build_reports_for_briefing_artifact_run_uuid(
        briefing_artifact_run_uuid,
        include_ledger_rows=include_ledger_rows,
        stale_after_minutes=stale_after_minutes,
    )
    if response.count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Roundtrip reports not found for "
                f"briefing_artifact_run_uuid={briefing_artifact_run_uuid}"
            ),
        )
    return response


@router.get(
    "/api/alpaca-paper/ledger/{ledger_id}",
    response_model=AlpacaPaperOrderLedgerRead,
)
async def get_ledger_by_id(
    ledger_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> AlpacaPaperOrderLedgerRead:
    svc = AlpacaPaperLedgerService(db)
    row = await svc.get_by_id(ledger_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ledger entry not found for id={ledger_id}",
        )
    return AlpacaPaperOrderLedgerRead.model_validate(row)
