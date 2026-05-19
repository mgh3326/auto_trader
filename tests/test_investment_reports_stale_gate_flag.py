"""ROB-269 Phase 3 — ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED wiring.

Review-pass fix for PR #876 item 4 — the flag now controls service-side
gate enforcement during ``InvestmentReportIngestionService.ingest``:

* flag=False (default): gate is purely advisory; result attached to
  ``report_metadata.stale_gate`` for audit but the row is always inserted.
* flag=True: blocking results raise ``StaleGateRejection`` before insert.
* Legacy reports (no ``snapshot_freshness_summary``) and informational
  reports (``account_scope is None``) bypass both layers — the gate is a
  no-op regardless of flag state.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.investment_reports import IngestReportRequest
from app.services.action_report.common.bundle_aware_publishing import (
    StaleGateRejection,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)


def _base_request(**overrides: Any) -> IngestReportRequest:
    payload: dict = {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_live",
        "execution_mode": "advisory_only",  # required for kis_live
        "created_by_profile": "test",
        "title": "test report",
        "summary": "no executable verbs in plain ascii",
        "kst_date": f"2026-05-{19 + len(overrides):02d}",
        "generator_version": "v1",
    }
    payload.update(overrides)
    return IngestReportRequest(**payload)


# ---------------------------------------------------------------------------
# flag=False (default) — gate is advisory only
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_disabled_advisory_only_lets_violating_text_through(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag=False: violating text + hard_stale freshness → row still inserts;
    the rejection is recorded under report_metadata.stale_gate.reject=True."""
    monkeypatch.setattr(
        settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", False
    )

    request = _base_request(
        title="삼성전자 매수 검토",  # forbidden verb when stale
        snapshot_bundle_uuid=uuid.uuid4(),
        snapshot_policy_version="intraday_action_report_v1",
        snapshot_freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "hard_stale"},  # critical kind degraded
        },
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()

    # Row exists.
    assert report.id > 0
    # Stale-gate result attached to metadata for audit.
    gate = report.report_metadata.get("stale_gate")
    assert gate is not None
    assert gate["reject"] is True
    assert gate["constraints"]["allow_action_language"] is False
    assert gate["lint"]["ok"] is False


@pytest.mark.asyncio
async def test_flag_disabled_clean_text_records_non_reject(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag=False: clean text + fresh critical kinds → reject=False audit."""
    monkeypatch.setattr(
        settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", False
    )

    request = _base_request(
        snapshot_freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()

    gate = report.report_metadata.get("stale_gate")
    assert gate is not None
    assert gate["reject"] is False


# ---------------------------------------------------------------------------
# flag=True — gate enforces; blocking results raise before insert
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_enabled_raises_on_violating_text_with_stale_freshness(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag=True: hard_stale critical kind + forbidden verb in text →
    StaleGateRejection BEFORE insert. No row is written."""
    monkeypatch.setattr(settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", True)

    request = _base_request(
        title="삼성전자 매수 검토",
        snapshot_bundle_uuid=uuid.uuid4(),
        snapshot_freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "hard_stale"},
        },
    )
    svc = InvestmentReportIngestionService(session)
    with pytest.raises(StaleGateRejection) as excinfo:
        await svc.ingest(request)
    await session.rollback()

    # The exception carries the full result so callers can introspect.
    assert excinfo.value.result.reject is True
    assert excinfo.value.result.constraints.allow_action_language is False


@pytest.mark.asyncio
async def test_flag_enabled_succeeds_when_critical_kinds_fresh_and_text_clean(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag=True: clean text + fresh critical kinds → ingestion succeeds."""
    monkeypatch.setattr(settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", True)

    request = _base_request(
        title="market report",  # no verbs
        summary="ascii summary, no Korean action words",
        snapshot_freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.id > 0


@pytest.mark.asyncio
async def test_flag_enabled_bypasses_legacy_reports_without_snapshot_metadata(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag=True + legacy report (no snapshot_freshness_summary): bypass.
    Even forbidden verbs in the title get through because the gate has no
    bundle context to evaluate."""
    monkeypatch.setattr(settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", True)

    request = _base_request(
        title="매수 검토",  # would be blocked if bundle metadata were present
        # snapshot_freshness_summary intentionally absent
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.id > 0


@pytest.mark.asyncio
async def test_flag_enabled_bypasses_informational_reports_with_no_account_scope(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag=True + account_scope=None: bypass (no broker context)."""
    monkeypatch.setattr(settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", True)

    request = _base_request(
        title="매수 검토",
        account_scope=None,  # informational
        execution_mode="advisory_only",
        snapshot_freshness_summary={
            "overall": "hard_stale",
            "portfolio": {"status": "hard_stale"},
        },
        # Draft so we don't trip the DB CHECK as an unrelated failure mode.
        status="draft",
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.id > 0


# ---------------------------------------------------------------------------
# Metadata audit preservation — caller-supplied keys are not overwritten
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_caller_supplied_stale_gate_metadata_key_is_preserved(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a caller already supplied a ``stale_gate`` key in metadata (e.g.
    they ran the gate themselves), the ingestion service must not overwrite
    it. The service uses setdefault, so caller wins."""
    monkeypatch.setattr(
        settings, "ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED", False
    )

    caller_marker = {"caller_supplied": True, "version": "external"}
    request = _base_request(
        metadata={"stale_gate": caller_marker},
        snapshot_freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
        },
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.report_metadata["stale_gate"] == caller_marker
