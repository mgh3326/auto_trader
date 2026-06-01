"""ROB-287 — Hermes MCP wiring round-trip tests.

Covers all four Hermes MCP tools (PRs #901 + #905):

* ``investment_report_prepare_bundle``
* ``investment_report_get_hermes_context``
* ``investment_stage_artifacts_ingest_from_hermes``
* ``investment_report_create_from_hermes_composition``

All four tools are gated by ``settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``
— the gate-off path returns a structured ``success=False`` envelope. Gate-on
paths exercise the underlying service layer (``HermesContextExporter`` /
``HermesCompositionIngestService`` / ``HermesStageArtifactsIngestService``)
via patched async sessions or mocked services so the suite stays
unit-shaped — the deep service-level append-only tests for the
stage-artifacts ingest live in
``tests/services/investment_stages/test_hermes_stage_artifacts_ingest.py``
and use the real ``db_session`` fixture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.mcp_server.tooling.investment_hermes_handlers import (
    INVESTMENT_HERMES_TOOL_NAMES,
    investment_report_create_from_hermes_composition_impl,
    investment_report_get_hermes_context_impl,
    investment_report_prepare_bundle_impl,
    investment_stage_artifacts_ingest_from_hermes_impl,
    investment_report_prepare_intraday_context_impl,
    register_investment_hermes_tools,
)


@dataclass
class _RecordedTool:
    name: str
    description: str
    func: Any


@dataclass
class _RecorderMcp:
    """Stand-in for ``FastMCP`` that captures ``mcp.tool(...)`` registrations."""

    registered: list[_RecordedTool] = field(default_factory=list)

    def tool(self, *, name: str, description: str):
        def _decorator(func):
            self.registered.append(
                _RecordedTool(name=name, description=description, func=func)
            )
            return func

        return _decorator


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_register_investment_hermes_tools_adds_expected_names() -> None:
    mcp = _RecorderMcp()
    register_investment_hermes_tools(mcp)
    assert {t.name for t in mcp.registered} == INVESTMENT_HERMES_TOOL_NAMES


def test_investment_hermes_tool_names_lock() -> None:
    # Locked surface — adding/renaming a tool here should be a deliberate change.
    assert INVESTMENT_HERMES_TOOL_NAMES == {
        "investment_report_prepare_bundle",
        "investment_report_get_hermes_context",
        "investment_report_create_from_hermes_composition",
        "investment_stage_artifacts_ingest_from_hermes",
        "investment_report_prepare_intraday_context",
    }


def test_tool_descriptions_advertise_no_internal_llm_or_mutation() -> None:
    """The advertised tool descriptions must call out the safety boundary."""
    mcp = _RecorderMcp()
    register_investment_hermes_tools(mcp)
    by_name = {t.name: t.description for t in mcp.registered}
    assert "no in-process LLM" in by_name["investment_report_prepare_bundle"]
    assert (
        "no broker / order / watch / order-intent side effect"
        in by_name["investment_report_prepare_bundle"]
    )
    assert (
        "Read-only" in by_name["investment_report_get_hermes_context"]
        or "read-only" in by_name["investment_report_get_hermes_context"]
    )
    assert (
        "requires_user_approval"
        in by_name["investment_report_create_from_hermes_composition"]
    )
    # New stage-artifacts ingest tool advertises append-only + no-side-effect.
    stage_desc = by_name["investment_stage_artifacts_ingest_from_hermes"]
    assert "append-only" in stage_desc.lower()
    assert "no broker / order / watch / order-intent side effect" in stage_desc.lower()


# ---------------------------------------------------------------------------
# Gate-off envelopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_bundle_disabled_without_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    result = await investment_report_prepare_bundle_impl(
        market="crypto",
        account_scope="upbit_live",
    )
    assert result["success"] is False
    assert result["error"] == "snapshot_backed_report_generator_disabled"
    assert "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED" in result["hint"]


@pytest.mark.asyncio
async def test_get_hermes_context_disabled_without_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    result = await investment_report_get_hermes_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
    )
    assert result["success"] is False
    assert result["error"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_stage_artifacts_ingest_disabled_without_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    result = await investment_stage_artifacts_ingest_from_hermes_impl(
        run_envelope={
            "run_uuid": str(uuid.uuid4()),
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "market": "kr",
        },
        artifacts=[
            {
                "stage_type": "market",
                "verdict": "neutral",
                "confidence": 50,
            }
        ],
    )
    assert result["success"] is False
    assert result["error"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_stage_artifacts_ingest_invalid_envelope_returns_structured_error(
    monkeypatch,
) -> None:
    """TS11 — envelope/schema validation errors come back as structured
    ``invalid_stage_artifacts_request`` envelopes, not exceptions."""
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    # Missing required ``run_uuid`` in run_envelope.
    result = await investment_stage_artifacts_ingest_from_hermes_impl(
        run_envelope={
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "market": "kr",
        },
        artifacts=[
            {
                "stage_type": "market",
                "verdict": "neutral",
                "confidence": 50,
            }
        ],
    )
    assert result["success"] is False
    assert result["error"] == "invalid_stage_artifacts_request"


@pytest.mark.asyncio
async def test_create_from_hermes_composition_disabled_without_flag(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    result = await investment_report_create_from_hermes_composition_impl(
        composition={
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "hermes_run_id": "x",
            "title": "t",
            "summary": "s",
            "items": [],
        },
        kst_date="2026-05-21",
        market="crypto",
    )
    assert result["success"] is False
    assert result["error"] == "snapshot_backed_report_generator_disabled"


# ---------------------------------------------------------------------------
# Gate-on round-trip — patched services
# ---------------------------------------------------------------------------


def _make_bundle(bundle_uuid: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        bundle_uuid=bundle_uuid,
        coverage_summary={"news": {"status": "fresh"}},
        freshness_summary={"overall": "fresh"},
        status="complete",
        market="crypto",
        account_scope="upbit_live",
        policy_version="intraday_action_report_v1",
    )


def _make_advisory_item(*, key: str = "auto-buy-BTC") -> dict[str, Any]:
    return {
        "client_item_key": key,
        "item_kind": "action",
        "operation": "review",
        "symbol": "BTC",
        "side": "buy",
        "intent": "buy_review",
        "rationale": "hermes-produced rationale",
        "apply_policy": "requires_user_approval",
    }


@pytest.fixture
def _flag_on(monkeypatch):
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )


class _FakeAsyncSession:
    """Minimal async context manager that yields itself."""

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def _patched_session_local():
    return _FakeAsyncSession()


@pytest.mark.asyncio
async def test_prepare_bundle_routes_through_ensure_service(_flag_on) -> None:
    bundle_uuid = uuid.uuid4()
    ensure_response = SimpleNamespace(
        bundle_uuid=bundle_uuid,
        status="complete",
        coverage_summary={"news": {"status": "fresh"}},
        freshness_summary={"overall": "fresh"},
        missing_sources=[],
        warnings=[],
        created=False,
    )
    ensure_response.model_dump = lambda mode="json": {
        "bundle_uuid": str(bundle_uuid),
        "status": "complete",
        "coverage_summary": {"news": {"status": "fresh"}},
        "freshness_summary": {"overall": "fresh"},
        "missing_sources": [],
        "warnings": [],
        "created": False,
    }

    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=ensure_response)

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.production_collector_registry",
            return_value=object(),
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ),
    ):
        result = await investment_report_prepare_bundle_impl(
            market="crypto",
            account_scope="upbit_live",
        )

    assert result["success"] is True
    assert result["bundle_uuid"] == str(bundle_uuid)
    assert result["status"] == "complete"
    ensure_svc.ensure.assert_awaited_once()
    called_request = ensure_svc.ensure.call_args.args[0]
    assert called_request.purpose == "report_generation"
    assert called_request.market == "crypto"
    assert called_request.account_scope == "upbit_live"
    assert called_request.requested_by == "hermes"


@pytest.mark.asyncio
async def test_prepare_bundle_injects_production_registry_and_user_id(_flag_on) -> None:
    bundle_uuid = uuid.uuid4()
    ensure_response = SimpleNamespace(
        bundle_uuid=bundle_uuid,
        status="complete",
        coverage_summary={},
        freshness_summary={},
        missing_sources=[],
        warnings=[],
        created=True,
    )
    ensure_response.model_dump = lambda mode="json": {
        "bundle_uuid": str(bundle_uuid),
        "status": "complete",
        "coverage_summary": {},
        "freshness_summary": {},
        "missing_sources": [],
        "warnings": [],
        "created": True,
    }
    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=ensure_response)
    sentinel_registry = object()

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.production_collector_registry",
            return_value=sentinel_registry,
        ) as mock_registry,
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ) as mock_cls,
    ):
        result = await investment_report_prepare_bundle_impl(
            market="kr",
            account_scope="kis_live",
            symbols=["005930"],
            user_id=7,
        )

    assert result["success"] is True
    mock_registry.assert_called_once()
    assert mock_cls.call_args.kwargs["collectors"] is sentinel_registry
    called_request = ensure_svc.ensure.call_args.args[0]
    assert called_request.user_id == 7
    assert called_request.market == "kr"
    assert called_request.account_scope == "kis_live"


@pytest.mark.asyncio
async def test_get_hermes_context_invalid_uuid(_flag_on) -> None:
    result = await investment_report_get_hermes_context_impl(
        snapshot_bundle_uuid="not-a-uuid"
    )
    assert result == {
        "success": False,
        "error": "invalid_uuid",
        "snapshot_bundle_uuid": "not-a-uuid",
    }


@pytest.mark.asyncio
async def test_get_hermes_context_missing_bundle(_flag_on) -> None:
    from app.services.investment_stages.hermes_context import (
        HermesContextExportError,
    )

    bundle_uuid = uuid.uuid4()
    exporter = AsyncMock()
    exporter.export = AsyncMock(side_effect=HermesContextExportError("missing"))

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.HermesContextExporter",
            return_value=exporter,
        ),
    ):
        result = await investment_report_get_hermes_context_impl(
            snapshot_bundle_uuid=str(bundle_uuid)
        )

    assert result["success"] is False
    assert result["error"] == "snapshot_bundle_not_found"
    assert result["snapshot_bundle_uuid"] == str(bundle_uuid)


@pytest.mark.asyncio
async def test_get_hermes_context_returns_payload_on_success(_flag_on) -> None:
    from app.schemas.hermes_composition import HermesContextPayload

    bundle_uuid = uuid.uuid4()
    payload = HermesContextPayload(
        snapshot_bundle_uuid=bundle_uuid,
        bundle_status="complete",
        market="crypto",
        account_scope="upbit_live",
        policy_version="intraday_action_report_v1",
    )

    exporter = AsyncMock()
    exporter.export = AsyncMock(return_value=payload)

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.HermesContextExporter",
            return_value=exporter,
        ),
    ):
        result = await investment_report_get_hermes_context_impl(
            snapshot_bundle_uuid=str(bundle_uuid)
        )

    assert result["success"] is True
    assert result["context_version"] == "hermes-context.v1"
    assert result["snapshot_bundle_uuid"] == str(bundle_uuid)
    assert result["bundle_status"] == "complete"
    assert result["constraints"]["advisory_only"] is True


@pytest.mark.asyncio
async def test_create_from_hermes_composition_rejects_invalid_envelope(
    _flag_on,
) -> None:
    """Composition validator rejects items that violate advisory-only invariants."""
    result = await investment_report_create_from_hermes_composition_impl(
        composition={
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "hermes_run_id": "run-1",
            "title": "Bad",
            "summary": "Bad",
            "items": [
                {
                    "client_item_key": "bad-op",
                    "item_kind": "action",
                    "operation": "create",
                    "intent": "buy_review",
                    "symbol": "BTC",
                    "side": "buy",
                    "rationale": "x",
                    "apply_policy": "requires_user_approval",
                }
            ],
        },
        kst_date="2026-05-21",
        market="crypto",
    )
    assert result["success"] is False
    assert result["error"] == "invalid_hermes_composition"
    assert "advisory-only" in result["detail"]


@pytest.mark.asyncio
async def test_create_from_hermes_composition_missing_bundle(_flag_on) -> None:
    from app.services.investment_stages.hermes_ingest import (
        HermesCompositionIngestError,
    )

    bundle_uuid = uuid.uuid4()
    svc = AsyncMock()
    svc.ingest_composition = AsyncMock(
        side_effect=HermesCompositionIngestError("missing bundle")
    )

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.HermesCompositionIngestService",
            return_value=svc,
        ),
    ):
        result = await investment_report_create_from_hermes_composition_impl(
            composition={
                "snapshot_bundle_uuid": str(bundle_uuid),
                "hermes_run_id": "run-1",
                "title": "t",
                "summary": "s",
                "items": [_make_advisory_item()],
            },
            kst_date="2026-05-21",
            market="crypto",
            account_scope="upbit_live",
        )

    assert result["success"] is False
    assert result["error"] == "snapshot_bundle_not_found"
    assert result["snapshot_bundle_uuid"] == str(bundle_uuid)


@pytest.mark.asyncio
async def test_create_from_hermes_composition_happy_path(_flag_on) -> None:
    bundle_uuid = uuid.uuid4()
    report_uuid = uuid.uuid4()
    report = SimpleNamespace(
        report_uuid=report_uuid,
        idempotency_key="idem-1",
        status="draft",
    )
    svc = AsyncMock()
    svc.ingest_composition = AsyncMock(return_value=report)

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.HermesCompositionIngestService",
            return_value=svc,
        ),
    ):
        result = await investment_report_create_from_hermes_composition_impl(
            composition={
                "snapshot_bundle_uuid": str(bundle_uuid),
                "hermes_run_id": "run-1",
                "title": "Hermes Advisory",
                "summary": "Synth",
                "items": [_make_advisory_item()],
            },
            kst_date="2026-05-21",
            market="crypto",
            account_scope="upbit_live",
        )

    assert result["success"] is True
    assert result["report_uuid"] == str(report_uuid)
    assert result["idempotency_key"] == "idem-1"
    assert result["snapshot_bundle_uuid"] == str(bundle_uuid)
    assert result["status"] == "draft"
    assert result["items_count"] == 1
    svc.ingest_composition.assert_awaited_once()
    envelope = svc.ingest_composition.call_args.args[0]
    assert envelope.composition.snapshot_bundle_uuid == bundle_uuid
    assert envelope.market == "crypto"
    assert envelope.generator_version == "hermes-composition.v1"


@pytest.mark.asyncio
async def test_create_from_hermes_composition_idempotency_reroutes_existing(
    _flag_on,
) -> None:
    """Two invocations with the same envelope return the same report; the
    ingest service is responsible for the actual idempotency-key check via
    InvestmentReportIngestionService."""
    bundle_uuid = uuid.uuid4()
    report_uuid = uuid.uuid4()
    report = SimpleNamespace(
        report_uuid=report_uuid,
        idempotency_key="idem-1",
        status="draft",
    )
    svc = AsyncMock()
    svc.ingest_composition = AsyncMock(return_value=report)

    composition_payload = {
        "snapshot_bundle_uuid": str(bundle_uuid),
        "hermes_run_id": "run-1",
        "title": "Hermes Advisory",
        "summary": "Synth",
        "items": [_make_advisory_item()],
    }

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.HermesCompositionIngestService",
            return_value=svc,
        ),
    ):
        first = await investment_report_create_from_hermes_composition_impl(
            composition=composition_payload,
            kst_date="2026-05-21",
            market="crypto",
            account_scope="upbit_live",
        )
        second = await investment_report_create_from_hermes_composition_impl(
            composition=composition_payload,
            kst_date="2026-05-21",
            market="crypto",
            account_scope="upbit_live",
        )

    assert first == second
    assert first["report_uuid"] == str(report_uuid)
    assert svc.ingest_composition.await_count == 2


@pytest.mark.asyncio
async def test_create_from_hermes_composition_zero_items_partial_data(_flag_on) -> None:
    """Partial data: Hermes may legitimately return zero items when the
    bundle is too thin. The MCP tool must accept and persist that envelope."""
    bundle_uuid = uuid.uuid4()
    report = SimpleNamespace(
        report_uuid=uuid.uuid4(),
        idempotency_key="idem-partial",
        status="draft",
    )
    svc = AsyncMock()
    svc.ingest_composition = AsyncMock(return_value=report)

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.HermesCompositionIngestService",
            return_value=svc,
        ),
    ):
        result = await investment_report_create_from_hermes_composition_impl(
            composition={
                "snapshot_bundle_uuid": str(bundle_uuid),
                "hermes_run_id": "thin-bundle",
                "title": "Hermes Advisory",
                "summary": "Nothing actionable",
                "items": [],
            },
            kst_date="2026-05-21",
            market="crypto",
            account_scope="upbit_live",
        )

    assert result["success"] is True
    assert result["items_count"] == 0


@pytest.mark.asyncio
async def test_prepare_intraday_context_disabled(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False)
    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(uuid.uuid4()),
    )
    assert out["success"] is False
    assert out["error"] == "snapshot_backed_report_generator_disabled"


def _fake_payload() -> object:
    from app.schemas.hermes_composition import HermesContextPayload

    return HermesContextPayload(
        snapshot_bundle_uuid=uuid.uuid4(),
        bundle_status="ready",
        market="us",
        policy_version="intraday_action_report_v1",
    )


@pytest.mark.asyncio
async def test_prepare_intraday_context_attaches_delta(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)
    base_uuid = uuid.uuid4()

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    class _FakeDelta:
        def __init__(self, db): ...
        async def compute_delta(self, report_uuid, **kw):
            return {"success": True, "baseline_report_uuid": str(report_uuid),
                    "levels_delta": {"summary": {"target_hit": 1}}}

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)
    monkeypatch.setattr(h, "DeltaService", _FakeDelta)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(base_uuid),
    )
    assert out["success"] is True
    assert out["report_type_hint"] == "intraday_update_v1"
    assert out["baseline_report_uuid"] == str(base_uuid)
    assert out["intraday_delta_block"]["success"] is True
    assert out["intraday_delta_block"]["levels_delta"]["summary"]["target_hit"] == 1


@pytest.mark.asyncio
async def test_prepare_intraday_context_failopen_bad_baseline(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    class _FakeDelta:
        def __init__(self, db): ...
        async def compute_delta(self, report_uuid, **kw):
            return {"success": False, "error": "baseline_not_found"}

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)
    monkeypatch.setattr(h, "DeltaService", _FakeDelta)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(uuid.uuid4()),
    )
    # fail-open: context still success, delta block carries the reason
    assert out["success"] is True
    assert out["intraday_delta_block"]["success"] is False
    assert out["intraday_delta_block"]["error"] == "baseline_not_found"


@pytest.mark.asyncio
async def test_prepare_intraday_context_failopen_delta_raises(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    class _FakeDelta:
        def __init__(self, db): ...
        async def compute_delta(self, report_uuid, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)
    monkeypatch.setattr(h, "DeltaService", _FakeDelta)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(uuid.uuid4()),
    )
    assert out["success"] is True
    assert "unavailable" in out["intraday_delta_block"]


@pytest.mark.asyncio
async def test_prepare_intraday_context_invalid_baseline_uuid(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid="not-a-uuid",
    )
    assert out["success"] is True
    assert out["intraday_delta_block"]["error"] == "invalid_report_uuid"
