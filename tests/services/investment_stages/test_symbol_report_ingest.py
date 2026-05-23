"""ROB-301 T3 — SymbolIntermediateReportIngestService tests.

Pure: verdict derivation table (D11). DB-backed: happy 2-symbol ingest,
idempotent rerun, version bump on changed content, unavailable (data_available
False) -> deferred/unavailable, open_action-without-side rejection, run/bundle
not-found. Snapshot bundle repo is mocked (bundle existence is tangential);
the stage run is created on the real session because the service requires it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.hermes_composition import HermesStageRunEnvelope
from app.schemas.investment_symbol_reports import (
    HermesSymbolReductionResult,
    HermesSymbolReportsIngestRequest,
)
from app.services.investment_stages.repository import InvestmentStagesRepository
from app.services.investment_stages.symbol_report_ingest import (
    SymbolIntermediateReportIngestService,
    SymbolReportIngestError,
    derive_verdict,
)

MARKET = "kr"
SESSION = "regular"
SCOPE = "kis_live"


def _snapshots_repo(bundle_uuid: uuid.UUID | None) -> AsyncMock:
    repo = AsyncMock()
    row = None if bundle_uuid is None else SimpleNamespace(bundle_uuid=bundle_uuid)
    repo.get_bundle_by_uuid = AsyncMock(return_value=row)
    return repo


async def _make_run(db_session, bundle_uuid: uuid.UUID) -> uuid.UUID:
    run = await InvestmentStagesRepository(db_session).create_run(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle_uuid,
        market=MARKET,
        market_session=SESSION,
        account_scope=SCOPE,
    )
    return run.run_uuid


def _envelope(
    run_uuid: uuid.UUID, bundle_uuid: uuid.UUID
) -> HermesStageRunEnvelope:
    return HermesStageRunEnvelope(
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle_uuid,
        market=MARKET,
        market_session=SESSION,
        account_scope=SCOPE,
    )


def _result(**overrides) -> HermesSymbolReductionResult:
    base = {
        "symbol": "005930.KS",
        "decision_bucket": "new_buy_candidate",
        "side": "buy",
        "rationale": "fresh quote",
        "buy_evidence": [{"snapshot_uuid": str(uuid.uuid4())}],
    }
    base.update(overrides)
    return HermesSymbolReductionResult.model_validate(base)


def _request(run_uuid, bundle_uuid, results) -> HermesSymbolReportsIngestRequest:
    return HermesSymbolReportsIngestRequest(
        run_envelope=_envelope(run_uuid, bundle_uuid), symbol_reports=results
    )


# --- Pure: verdict derivation table (D11) ---


@pytest.mark.parametrize(
    "bucket,side,expected",
    [
        ("new_buy_candidate", "buy", "buy"),
        ("completed_or_existing", None, "hold"),
        ("deferred_no_action", None, "hold"),
        ("open_action", "buy", "buy"),
        ("open_action", "sell", "sell"),
        ("risk_watch", "sell", "sell"),
        ("risk_watch", None, "risk"),
    ],
)
def test_derive_verdict_table(bucket, side, expected):
    payload = _result(decision_bucket=bucket, side=side)
    verdict, out_bucket, reason = derive_verdict(payload)
    assert verdict == expected
    assert out_bucket == bucket
    assert reason is None


def test_derive_verdict_unavailable():
    payload = HermesSymbolReductionResult.model_validate(
        {"symbol": "X.KS", "data_available": False}
    )
    assert derive_verdict(payload) == (
        "unavailable",
        "deferred_no_action",
        "data_unavailable",
    )


def test_derive_verdict_open_action_without_side_raises():
    payload = _result(decision_bucket="open_action", side=None)
    with pytest.raises(SymbolReportIngestError) as exc:
        derive_verdict(payload)
    assert exc.value.code == "open_action_missing_side"


# --- DB-backed ingest ---


@pytest.mark.asyncio
async def test_happy_two_symbols(db_session):
    bundle_uuid = uuid.uuid4()
    run_uuid = await _make_run(db_session, bundle_uuid)
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=_snapshots_repo(bundle_uuid)
    )
    req = _request(
        run_uuid,
        bundle_uuid,
        [
            _result(
                symbol="005930.KS", decision_bucket="new_buy_candidate", side="buy"
            ),
            _result(symbol="000660.KS", decision_bucket="open_action", side="sell"),
        ],
    )
    resp = await svc.ingest_from_hermes(req)
    assert {r.symbol for r in resp.results} == {"005930.KS", "000660.KS"}
    assert all(not r.idempotent_existing for r in resp.results)
    by_symbol = {r.symbol: r.report for r in resp.results}
    assert by_symbol["005930.KS"].verdict == "buy"
    assert by_symbol["000660.KS"].verdict == "sell"  # open_action + side=sell
    assert by_symbol["005930.KS"].buy_evidence  # structured evidence persisted
    assert by_symbol["005930.KS"].content_hash


@pytest.mark.asyncio
async def test_idempotent_rerun(db_session):
    bundle_uuid = uuid.uuid4()
    run_uuid = await _make_run(db_session, bundle_uuid)
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=_snapshots_repo(bundle_uuid)
    )
    req = _request(run_uuid, bundle_uuid, [_result()])
    first = await svc.ingest_from_hermes(req)
    second = await svc.ingest_from_hermes(req)
    assert first.results[0].idempotent_existing is False
    assert second.results[0].idempotent_existing is True
    assert (
        first.results[0].report.symbol_report_uuid
        == second.results[0].report.symbol_report_uuid
    )


@pytest.mark.asyncio
async def test_version_bump_on_changed_content(db_session):
    bundle_uuid = uuid.uuid4()
    run_uuid = await _make_run(db_session, bundle_uuid)
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=_snapshots_repo(bundle_uuid)
    )
    await svc.ingest_from_hermes(
        _request(run_uuid, bundle_uuid, [_result(rationale="A")])
    )
    resp2 = await svc.ingest_from_hermes(
        _request(run_uuid, bundle_uuid, [_result(rationale="B")])
    )
    assert resp2.results[0].idempotent_existing is False
    assert resp2.results[0].report.artifact_version == 2


@pytest.mark.asyncio
async def test_unavailable_symbol_persists_deferred(db_session):
    bundle_uuid = uuid.uuid4()
    run_uuid = await _make_run(db_session, bundle_uuid)
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=_snapshots_repo(bundle_uuid)
    )
    payload = HermesSymbolReductionResult.model_validate(
        {"symbol": "AAA.KS", "data_available": False}
    )
    resp = await svc.ingest_from_hermes(_request(run_uuid, bundle_uuid, [payload]))
    report = resp.results[0].report
    assert report.verdict == "unavailable"
    assert report.decision_bucket == "deferred_no_action"
    assert report.unavailable_reason == "data_unavailable"


@pytest.mark.asyncio
async def test_run_not_found(db_session):
    bundle_uuid = uuid.uuid4()
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=_snapshots_repo(bundle_uuid)
    )
    req = _request(uuid.uuid4(), bundle_uuid, [_result()])  # run never created
    with pytest.raises(SymbolReportIngestError) as exc:
        await svc.ingest_from_hermes(req)
    assert exc.value.code == "stage_run_not_found"


@pytest.mark.asyncio
async def test_bundle_not_found(db_session):
    bundle_uuid = uuid.uuid4()
    run_uuid = await _make_run(db_session, bundle_uuid)
    svc = SymbolIntermediateReportIngestService(
        db_session, snapshots_repository=_snapshots_repo(None)
    )
    req = _request(run_uuid, bundle_uuid, [_result()])
    with pytest.raises(SymbolReportIngestError) as exc:
        await svc.ingest_from_hermes(req)
    assert exc.value.code == "snapshot_bundle_not_found"
