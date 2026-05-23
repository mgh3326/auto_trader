"""ROB-301 T1 — InvestmentSymbolIntermediateReport model tests.

Covers: insert/defaults roundtrip, UNIQUE(run_uuid, symbol, report_kind,
artifact_version), FK to investment_stage_runs, the unavailable->deferred bucket
CHECK (D11), and the single-source enum drift guard (D5).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import CheckConstraint, select
from sqlalchemy.exc import IntegrityError

from app.models.investment_stages import InvestmentStageRun
from app.models.investment_symbol_intermediate_reports import (
    DECISION_BUCKETS,
    REPORT_KINDS,
    UNAVAILABLE_REASONS,
    VERDICTS,
    InvestmentSymbolIntermediateReport,
)


async def _make_run(db_session, *, market: str = "kr") -> InvestmentStageRun:
    run = InvestmentStageRun(
        snapshot_bundle_uuid=uuid.uuid4(),
        market=market,
        account_scope="kis_live",
    )
    db_session.add(run)
    await db_session.flush()
    return run


def _report(run, **overrides):
    fields = {
        "run_uuid": run.run_uuid,
        "snapshot_bundle_uuid": run.snapshot_bundle_uuid,
        "market": "kr",
        "account_scope": "kis_live",
        "symbol": "005930.KS",
        "symbol_name": "Samsung Electronics",
        "decision_bucket": "new_buy_candidate",
        "verdict": "buy",
        "content_hash": "hash-a",
        "idempotency_key": f"key-{uuid.uuid4()}",
        **overrides,
    }
    return InvestmentSymbolIntermediateReport(**fields)


@pytest.mark.asyncio
async def test_insert_returns_uuid_and_defaults(db_session):
    run = await _make_run(db_session)
    report = _report(run, buy_evidence=[{"snapshot_uuid": str(uuid.uuid4())}])
    db_session.add(report)
    await db_session.flush()

    assert report.symbol_report_uuid is not None
    assert report.report_kind == "final_report_symbol"  # server default
    assert report.artifact_version == 1  # server default
    assert report.source_stage_artifact_uuids == []
    assert report.cited_snapshot_uuids == []

    fetched = await db_session.scalar(
        select(InvestmentSymbolIntermediateReport).where(
            InvestmentSymbolIntermediateReport.run_uuid == run.run_uuid
        )
    )
    assert fetched is not None
    assert fetched.decision_bucket == "new_buy_candidate"
    assert fetched.buy_evidence == [
        {"snapshot_uuid": fetched.buy_evidence[0]["snapshot_uuid"]}
    ]


@pytest.mark.asyncio
async def test_unique_run_symbol_kind_version(db_session):
    run = await _make_run(db_session)
    db_session.add(_report(run, content_hash="h1", idempotency_key="k1"))
    await db_session.flush()
    # Same (run, symbol, report_kind, artifact_version=1 default) -> conflict.
    db_session.add(_report(run, content_hash="h2", idempotency_key="k2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_version_bump_allows_second_row(db_session):
    run = await _make_run(db_session)
    db_session.add(_report(run, artifact_version=1, idempotency_key="v1"))
    await db_session.flush()
    db_session.add(
        _report(run, artifact_version=2, idempotency_key="v2", content_hash="hash-b")
    )
    await db_session.flush()  # different version -> allowed (append-only history)
    rows = (
        await db_session.scalars(
            select(InvestmentSymbolIntermediateReport).where(
                InvestmentSymbolIntermediateReport.run_uuid == run.run_uuid
            )
        )
    ).all()
    assert {r.artifact_version for r in rows} == {1, 2}


@pytest.mark.asyncio
async def test_fk_requires_existing_run(db_session):
    orphan = InvestmentSymbolIntermediateReport(
        run_uuid=uuid.uuid4(),  # no such run
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        symbol="005930.KS",
        decision_bucket="new_buy_candidate",
        verdict="buy",
        content_hash="h",
        idempotency_key="orphan",
    )
    db_session.add(orphan)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unavailable_verdict_must_pin_deferred_bucket(db_session):
    run = await _make_run(db_session)
    bad = _report(
        run,
        verdict="unavailable",
        decision_bucket="risk_watch",  # violates D11 CHECK
        unavailable_reason="data_unavailable",
        idempotency_key="bad-unavail",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unavailable_verdict_with_deferred_bucket_ok(db_session):
    run = await _make_run(db_session)
    ok = _report(
        run,
        verdict="unavailable",
        decision_bucket="deferred_no_action",
        unavailable_reason="hermes_omitted",
        idempotency_key="ok-unavail",
    )
    db_session.add(ok)
    await db_session.flush()
    assert ok.symbol_report_uuid is not None


@pytest.mark.asyncio
async def test_decision_bucket_check_rejects_unknown(db_session):
    run = await _make_run(db_session)
    db_session.add(
        _report(run, decision_bucket="totally_made_up", idempotency_key="bad-bucket")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --- D5: single-source enum drift guard (pure unit test, no DB) ---


def _check_text_for(column_token: str) -> str:
    """Find the single CHECK whose SQL contains ``<column_token>`` (e.g.
    ``decision_bucket IN``). Located by SQL text, not name — the metadata
    naming_convention rewraps explicit CHECK names, so name lookup is unreliable.
    """
    matches = [
        str(c.sqltext)
        for c in InvestmentSymbolIntermediateReport.__table__.constraints
        if isinstance(c, CheckConstraint) and column_token in str(c.sqltext)
    ]
    assert len(matches) == 1, (
        f"expected exactly one CHECK with {column_token!r}, got {len(matches)}"
    )
    return matches[0]


def test_enum_single_source_no_drift():
    """Model CHECK constraints must reflect exactly the canonical tuples (D5)."""
    cases = {
        "decision_bucket IN": DECISION_BUCKETS,
        "verdict IN": VERDICTS,
        "report_kind IN": REPORT_KINDS,
        "unavailable_reason IN": UNAVAILABLE_REASONS,
    }
    for token, values in cases.items():
        text = _check_text_for(token)
        for v in values:
            assert f"'{v}'" in text, f"{v} missing from CHECK [{token}]"
        # No extra values smuggled in: exactly len(values) quoted literals.
        assert text.count("'") == 2 * len(values), (
            f"unexpected literals in CHECK [{token}]"
        )
