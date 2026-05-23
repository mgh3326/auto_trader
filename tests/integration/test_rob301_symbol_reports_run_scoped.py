"""ROB-301 T6 — symbol reports are run-scoped diagnostics (D4).

Symbol intermediate reports key on ``run_uuid`` and have NO foreign key to
``investment_reports``. So when the final report is blocked by the stale gate
(ROB-279) and never persisted, the per-symbol diagnostics remain inspectable
run-scoped — exactly the ROB-279 refinement #5 property, extended to the symbol
axis. This integration test ingests symbol reports for a run that has NO final
report row and asserts they are still queryable.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models.investment_reports import InvestmentReport
from app.schemas.hermes_composition import HermesStageRunEnvelope
from app.schemas.investment_symbol_reports import (
    HermesSymbolReductionResult,
    HermesSymbolReportsIngestRequest,
)
from app.services.investment_stages.repository import InvestmentStagesRepository
from app.services.investment_stages.symbol_report_ingest import (
    SymbolIntermediateReportIngestService,
)
from app.services.investment_stages.symbol_report_repository import (
    SymbolIntermediateReportRepository,
)


@pytest.mark.asyncio
async def test_symbol_reports_survive_without_final_report(db_session):
    bundle_uuid = uuid.uuid4()
    run = await InvestmentStagesRepository(db_session).create_run(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )

    snapshots = AsyncMock()
    snapshots.get_bundle_by_uuid = AsyncMock(
        return_value=SimpleNamespace(bundle_uuid=bundle_uuid)
    )
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=snapshots
    )

    request = HermesSymbolReportsIngestRequest(
        run_envelope=HermesStageRunEnvelope(
            run_uuid=run.run_uuid,
            snapshot_bundle_uuid=bundle_uuid,
            market="kr",
            market_session="regular",
            account_scope="kis_live",
        ),
        symbol_reports=[
            HermesSymbolReductionResult.model_validate(
                {
                    "symbol": "005930.KS",
                    "decision_bucket": "new_buy_candidate",
                    "side": "buy",
                }
            ),
            # Evidence genuinely unavailable for this one — still persisted.
            HermesSymbolReductionResult.model_validate(
                {"symbol": "000660.KS", "data_available": False}
            ),
        ],
    )
    await svc.ingest_from_hermes(request)
    await db_session.flush()

    # The final report was blocked / never created: no investment_reports row
    # references this run's bundle.
    report_count = await db_session.scalar(
        select(func.count())
        .select_from(InvestmentReport)
        .where(InvestmentReport.snapshot_bundle_uuid == bundle_uuid)
    )
    assert report_count == 0

    # ...yet the per-symbol diagnostics survive, queryable run-scoped (D4).
    rows = await SymbolIntermediateReportRepository(db_session).list_for_run(
        run.run_uuid
    )
    assert {r.symbol for r in rows} == {"005930.KS", "000660.KS"}
    unavailable = next(r for r in rows if r.symbol == "000660.KS")
    assert unavailable.verdict == "unavailable"
    assert unavailable.decision_bucket == "deferred_no_action"
    assert unavailable.unavailable_reason == "data_unavailable"
