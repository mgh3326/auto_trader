"""ROB-287 Phase C — Hermes ↔ auto_trader JSON-over-wire round-trip smoke.

End-to-end exercise of the four Hermes endpoints in the order Hermes
would actually drive them in production:

    1. ``POST /hermes/context``         → frozen HermesContextPayload
    2. ``POST /hermes/stage-artifacts`` → append-only stage rows
    3. ``POST /hermes/composition``     → InvestmentReport row +
                                          auto-finalize the stage run
    4. (idempotency) repeat step 2 with identical payload → 200 OK
                     idempotent reroute

Uses the real ``db_session`` fixture so the AppendOnly + UNIQUE
constraints are exercised, with an in-process ASGI client driving
the HTTP router. AuthMiddleware is wired in to validate the token
gate on at least one request; the rest of the chain runs with the
token already approved.

The snapshot bundle is seeded directly in the test DB; the
``/prepare-bundle`` step is exercised as a separate dedicated test
with a mocked ensure service (because in-process bundle preparation
requires the full collector registry, which is out of scope for a
contract round-trip).

No external LLM is called. The artifacts + composition payloads are
loaded from ``tests/fixtures/hermes/*.json`` and represent the shape
Hermes is expected to produce.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.models.investment_reports import InvestmentReport
from app.models.investment_snapshots import InvestmentSnapshotBundle
from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun
from app.routers.dependencies import get_authenticated_user
from app.routers.investment_dimension_reports import (
    router as dimension_reports_router,
)
from app.routers.investment_hermes_http import router as hermes_router
from app.routers.investment_reports import router as investment_reports_router

# Cross-domain ``db_session`` test that writes to ``review.investment_reports``.
# Without this fixture, xdist sibling workers running ``investment_reports_helpers``
# can TRUNCATE the table between our ingest and our follow-up SELECT, returning
# ``None`` for a row we just persisted. ``investment_reports_cleanup_lock``
# serializes the cleanup window for the lifetime of the test, mirroring
# ``tests._investment_reports_helpers.session``'s own advisory-lock guard. The
# fix applies to both KR and US round-trip cases — KR happened to pass on the
# original session by timing, US flushed the race because the second smoke
# tightens the read window.
pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "hermes"
_TOKEN = "hermes-smoke-secret-1234"


def _load_fixture(name: str) -> dict[str, Any]:
    text = (_FIXTURE_DIR / name).read_text(encoding="utf-8")
    parsed = json.loads(text)
    parsed.pop("_comment", None)
    return parsed


def _substitute_placeholders(
    payload: dict[str, Any],
    *,
    run_uuid: uuid.UUID,
    snapshot_bundle_uuid: uuid.UUID,
    symbol_report_uuid: uuid.UUID | str | None = None,
    dimension_report_uuid: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """String substitution for the placeholder UUIDs inside a fixture.

    ``run_uuid`` / ``snapshot_bundle_uuid`` are always known. The
    ``symbol_report_uuid`` / ``dimension_report_uuid`` placeholders (ROB-309)
    are only known after the symbol-reports / dimension-reports ingests return
    their server-assigned UUIDs, so they are substituted in a second pass on
    the composition payload.
    """
    raw = json.dumps(payload)
    raw = raw.replace("{{run_uuid}}", str(run_uuid))
    raw = raw.replace("{{snapshot_bundle_uuid}}", str(snapshot_bundle_uuid))
    if symbol_report_uuid is not None:
        raw = raw.replace("{{symbol_report_uuid}}", str(symbol_report_uuid))
    if dimension_report_uuid is not None:
        raw = raw.replace("{{dimension_report_uuid}}", str(dimension_report_uuid))
    return json.loads(raw)


def _make_unique_for_test_db(
    payload: dict[str, Any], *, run_uuid: uuid.UUID
) -> dict[str, Any]:
    """Tweak the composition envelope so the report-idempotency key
    (``report_type + market + market_session + account_scope +
    execution_mode + kst_date + generator_version``) is unique per
    test invocation. The shared ``db_session`` fixture does not roll
    back between tests, so two tests reading the same fixture would
    otherwise share an idempotency-reused ``InvestmentReport`` row and
    fail downstream assertions about ``snapshot_bundle_uuid`` linkage.

    Mutates only ``generator_version`` (suffix with the run_uuid's
    short hex) — leaves the wire shape intact for fidelity to what
    Hermes would actually send."""
    payload = json.loads(json.dumps(payload))
    suffix = run_uuid.hex[:8]
    payload["generator_version"] = f"hermes-composition.v1-test-{suffix}"
    return payload


def _build_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(hermes_router)
    app.add_middleware(AuthMiddleware)

    async def _db_override() -> AsyncIterator[object]:
        yield db_session

    app.dependency_overrides[get_db] = _db_override
    return app


def _build_read_app(db_session) -> FastAPI:
    """ROB-309 — read-surface app (no AuthMiddleware).

    The dimension-reports read surface + the final report bundle GET are
    session-authed in production (NOT the Hermes ingest token), so they live
    on a separate app whose ``get_authenticated_user`` dependency is overridden
    with a stub user, mirroring the established pattern in the other
    ``/invest`` / ``/trading`` router tests. Shares the same ``db_session`` so
    rows written through the ingest app are visible here.
    """
    from types import SimpleNamespace

    app = FastAPI()
    app.include_router(investment_reports_router)
    app.include_router(dimension_reports_router)

    async def _db_override() -> AsyncIterator[object]:
        yield db_session

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    return app


@pytest.fixture
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    monkeypatch.setattr(settings, "HERMES_INGEST_TOKEN", _TOKEN, raising=False)
    monkeypatch.setattr(
        settings,
        "HERMES_INGEST_TOKEN_HEADER",
        "X-Hermes-Ingest-Token",
        raising=False,
    )


async def _seed_bundle(
    db_session, *, market: str, account_scope: str
) -> InvestmentSnapshotBundle:
    bundle = InvestmentSnapshotBundle(
        bundle_uuid=uuid.uuid4(),
        purpose="hermes_report_generation",
        market=market,
        account_scope=account_scope,
        policy_version="intraday_action_report_v1",
        status="complete",
        as_of=dt.datetime.now(tz=dt.UTC),
        coverage_summary={"required": {"portfolio": "fresh"}},
        freshness_summary={"overall": "fresh"},
        idempotency_key=f"smoke-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(bundle)
    await db_session.flush()
    await db_session.refresh(bundle)
    return bundle


@pytest_asyncio.fixture
async def _seeded_bundle(db_session) -> InvestmentSnapshotBundle:
    """Insert a minimal InvestmentSnapshotBundle row so the Hermes
    endpoints find it on bundle-UUID lookup. No items linked — the
    context exporter handles the empty-items case by emitting
    UNAVAILABLE stages, which is fine for the round-trip shape check.
    """
    bundle = InvestmentSnapshotBundle(
        bundle_uuid=uuid.uuid4(),
        purpose="hermes_report_generation",
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        status="complete",
        as_of=dt.datetime.now(tz=dt.UTC),
        coverage_summary={"required": {"portfolio": "fresh"}},
        freshness_summary={"overall": "fresh"},
        idempotency_key=f"smoke-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(bundle)
    await db_session.flush()
    await db_session.refresh(bundle)
    return bundle


def _auth_headers() -> dict[str, str]:
    return {"X-Hermes-Ingest-Token": _TOKEN}


# ---------------------------------------------------------------------------
# Round-trip happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roundtrip_context_artifacts_composition(
    db_session, _enabled, _seeded_bundle
) -> None:
    """Drive the FULL current contract (ROB-309): context →
    stage-artifacts → symbol-reports → dimension-reports → context
    re-pull → composition → read surfaces.

    Asserts:
    * /context returns a HermesContextPayload referencing the seeded bundle.
    * /stage-artifacts creates a stage run + 5 artifact rows.
    * /symbol-reports persists per-symbol reductions (ROB-301).
    * /dimension-reports persists per-dimension reports (ROB-306).
    * /context re-pull now carries non-empty ``dimension_reports`` +
      ``symbol_intermediate_reports``.
    * /composition stores the ``dimension_report_uuids`` +
      ``symbol_intermediate_report_uuids`` it consumed and classifies a
      ``new_buy_candidate`` item (ROB-308); the run auto-finalises (§D4).
    * The dimension-reports read surface + the final report bundle render
      the held-action vs new-candidate grouping.
    """
    bundle = _seeded_bundle
    run_uuid = uuid.uuid4()

    app = _build_app(db_session)
    read_app = _build_read_app(db_session)
    async with (
        AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client,
        AsyncClient(
            transport=ASGITransport(app=read_app), base_url="https://test"
        ) as read_client,
    ):
        # --- 1. context export ---
        ctx_resp = await client.post(
            "/trading/api/investment-reports/hermes/context",
            headers=_auth_headers(),
            json={"snapshot_bundle_uuid": str(bundle.bundle_uuid)},
        )
        assert ctx_resp.status_code == 200, ctx_resp.text
        ctx_body = ctx_resp.json()
        assert ctx_body["success"] is True
        assert ctx_body["context_version"] == "hermes-context.v1"
        assert ctx_body["snapshot_bundle_uuid"] == str(bundle.bundle_uuid)
        assert ctx_body["constraints"]["advisory_only"] is True
        # First pull (before any reports ingested) carries empty report lists.
        assert ctx_body["dimension_reports"] == []
        assert ctx_body["symbol_intermediate_reports"] == []
        # 5 deterministic stages render even with no snapshots (they
        # surface as UNAVAILABLE).
        stage_types = {entry["stage_type"] for entry in ctx_body["stage_inputs"]}
        assert {
            "market",
            "news",
            "portfolio_journal",
            "watch_context",
            "candidate_universe",
        }.issubset(stage_types)

        # --- 2. stage-artifacts ingest ---
        stage_payload = _substitute_placeholders(
            _load_fixture("stage_artifacts_request.json"),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle.bundle_uuid,
        )
        stage_resp = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts",
            headers=_auth_headers(),
            json=stage_payload,
        )
        assert stage_resp.status_code == 200, stage_resp.text
        stage_body = stage_resp.json()
        assert stage_body["success"] is True
        assert stage_body["run_uuid"] == str(run_uuid)
        assert stage_body["run_status"] == "running"
        assert len(stage_body["artifacts"]) == 5
        assert all(not r["idempotent_existing"] for r in stage_body["artifacts"])

        # --- 3. symbol-reports ingest (ROB-301) ---
        symbol_payload = _substitute_placeholders(
            _load_fixture("symbol_reports_request.json"),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle.bundle_uuid,
        )
        sym_resp = await client.post(
            "/trading/api/investment-reports/hermes/symbol-reports",
            headers=_auth_headers(),
            json=symbol_payload,
        )
        assert sym_resp.status_code == 200, sym_resp.text
        sym_body = sym_resp.json()
        assert sym_body["success"] is True
        assert sym_body["run_uuid"] == str(run_uuid)
        assert len(sym_body["symbol_reports"]) == 2
        assert all(not r["idempotent_existing"] for r in sym_body["symbol_reports"])
        symbol_report_uuid = sym_body["symbol_reports"][0]["symbol_report_uuid"]

        # --- 4. dimension-reports ingest (ROB-306) ---
        dimension_payload = _substitute_placeholders(
            _load_fixture("dimension_reports_request.json"),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle.bundle_uuid,
        )
        dim_resp = await client.post(
            "/trading/api/investment-reports/hermes/dimension-reports",
            headers=_auth_headers(),
            json=dimension_payload,
        )
        assert dim_resp.status_code == 200, dim_resp.text
        dim_body = dim_resp.json()
        assert dim_body["success"] is True
        assert dim_body["run_uuid"] == str(run_uuid)
        assert len(dim_body["dimension_reports"]) == 2
        market_dim = next(
            d for d in dim_body["dimension_reports"] if d["dimension"] == "market"
        )
        dimension_report_uuid = market_dim["dimension_report_uuid"]

        # --- 5. context re-pull carries both report families ---
        ctx2_resp = await client.post(
            "/trading/api/investment-reports/hermes/context",
            headers=_auth_headers(),
            json={"snapshot_bundle_uuid": str(bundle.bundle_uuid)},
        )
        assert ctx2_resp.status_code == 200, ctx2_resp.text
        ctx2_body = ctx2_resp.json()
        assert len(ctx2_body["dimension_reports"]) == 2, (
            "context re-pull must surface the freshly-ingested dimension reports"
        )
        assert len(ctx2_body["symbol_intermediate_reports"]) == 2, (
            "context re-pull must surface the freshly-ingested symbol reports"
        )

        # --- 6. composition ingest (threads the captured report UUIDs) ---
        composition_payload = _make_unique_for_test_db(
            _substitute_placeholders(
                _load_fixture("composition_request.json"),
                run_uuid=run_uuid,
                snapshot_bundle_uuid=bundle.bundle_uuid,
                symbol_report_uuid=symbol_report_uuid,
                dimension_report_uuid=dimension_report_uuid,
            ),
            run_uuid=run_uuid,
        )
        comp_resp = await client.post(
            "/trading/api/investment-reports/hermes/composition",
            headers=_auth_headers(),
            json=composition_payload,
        )
        assert comp_resp.status_code == 200, comp_resp.text
        comp_body = comp_resp.json()
        assert comp_body["success"] is True
        assert comp_body["status"] == "draft"
        assert comp_body["items_count"] == 3

        # --- Assertions on persisted state ---
        from sqlalchemy import select

        # Stage run finalised to ``completed`` (§D4 auto-finalize).
        run_row = await db_session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )
        assert run_row is not None, "stage run should exist"
        assert run_row.status == "completed", (
            f"composition ingest should auto-finalise the run; "
            f"got status={run_row.status!r}"
        )
        assert run_row.completed_at is not None

        # 5 stage artifacts persisted.
        artifact_rows = (
            await db_session.scalars(
                select(InvestmentStageArtifact).where(
                    InvestmentStageArtifact.run_uuid == run_uuid
                )
            )
        ).all()
        assert len(list(artifact_rows)) == 5

        # InvestmentReport row exists and references the bundle.
        report_uuid_str = comp_body["report_uuid"]
        report_uuid = uuid.UUID(report_uuid_str)
        report_row = await db_session.scalar(
            select(InvestmentReport).where(InvestmentReport.report_uuid == report_uuid)
        )
        assert report_row is not None
        assert report_row.snapshot_bundle_uuid == bundle.bundle_uuid
        # Stage-run linkage is preserved in metadata.
        hermes_meta = report_row.report_metadata.get("hermes_composition", {})
        assert hermes_meta.get("hermes_run_id") == "hermes-smoke-001"
        # ROB-308/ROB-301 — the consumed report UUIDs are threaded into metadata.
        assert report_row.report_metadata.get("symbol_intermediate_report_uuids") == [
            symbol_report_uuid
        ]
        assert report_row.report_metadata.get("dimension_report_uuids") == [
            dimension_report_uuid
        ]

        # --- 7. dimension-reports read surface (session-authed) ---
        dim_view_resp = await read_client.get(
            f"/trading/api/investment-reports/runs/{run_uuid}/dimension-reports",
            params={"dimension": "market"},
        )
        assert dim_view_resp.status_code == 200, dim_view_resp.text
        dim_view = dim_view_resp.json()
        assert dim_view["dimension"] == "market"
        assert len(dim_view["reports"]) == 1
        assert dim_view["reports"][0]["stance"] == "bullish"

        # --- 8. final report bundle grouping (held-action vs new-candidate) ---
        bundle_resp = await read_client.get(
            f"/trading/api/investment-reports/{report_uuid}"
        )
        assert bundle_resp.status_code == 200, bundle_resp.text
        bundle_body = bundle_resp.json()
        rollup = bundle_body["decision_rollup"]
        assert len(rollup["new_candidate"]) == 1, (
            "the new_buy_candidate item must classify into decision_rollup"
        )
        assert rollup["new_candidate"][0]["decision_bucket"] == "new_buy_candidate"
        assert rollup["new_candidate"][0]["cited_symbol_report_uuid"] == (
            symbol_report_uuid
        )
        assert rollup["new_candidate"][0]["cited_dimension_report_uuids"] == [
            dimension_report_uuid
        ]
        # item_groups keys items by decision_bucket; unclassified items land
        # under "unclassified".
        assert "new_buy_candidate" in bundle_body["item_groups"]
        assert "unclassified" in bundle_body["item_groups"]


@pytest.mark.asyncio
async def test_roundtrip_stage_artifacts_idempotent_reingest(
    db_session, _enabled, _seeded_bundle
) -> None:
    """Hermes re-posts an identical stage-artifacts payload → 200 OK
    with ``idempotent_existing=True`` for every artifact, no new
    rows."""
    bundle = _seeded_bundle
    run_uuid = uuid.uuid4()

    app = _build_app(db_session)
    payload = _substitute_placeholders(
        _load_fixture("stage_artifacts_request.json"),
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle.bundle_uuid,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        first = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts",
            headers=_auth_headers(),
            json=payload,
        )
        second = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts",
            headers=_auth_headers(),
            json=payload,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert all(not r["idempotent_existing"] for r in first_body["artifacts"])
    assert all(r["idempotent_existing"] for r in second_body["artifacts"])
    # Same artifact UUIDs returned on the second ingest.
    first_uuids = {r["artifact_uuid"] for r in first_body["artifacts"]}
    second_uuids = {r["artifact_uuid"] for r in second_body["artifacts"]}
    assert first_uuids == second_uuids


@pytest.mark.asyncio
async def test_roundtrip_stage_artifacts_content_conflict_rejected(
    db_session, _enabled, _seeded_bundle
) -> None:
    """Hermes re-posts a stage-artifacts payload with the SAME
    ``(run_uuid, stage_type)`` but different content → 409 with
    ``error='artifact_content_conflict'``."""
    bundle = _seeded_bundle
    run_uuid = uuid.uuid4()

    app = _build_app(db_session)
    base_payload = _substitute_placeholders(
        _load_fixture("stage_artifacts_request.json"),
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle.bundle_uuid,
    )

    # Subset: only the first artifact.
    first_payload = {
        "run_envelope": base_payload["run_envelope"],
        "artifacts": [base_payload["artifacts"][0]],
    }
    # Mutate verdict + confidence on the same stage_type.
    mutated_payload = json.loads(json.dumps(first_payload))
    mutated_payload["artifacts"][0]["verdict"] = "bear"
    mutated_payload["artifacts"][0]["confidence"] = 25

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        first = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts",
            headers=_auth_headers(),
            json=first_payload,
        )
        conflict = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts",
            headers=_auth_headers(),
            json=mutated_payload,
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "artifact_content_conflict"


@pytest.mark.asyncio
async def test_roundtrip_token_gate_blocks_unauthenticated(
    db_session, _enabled, _seeded_bundle
) -> None:
    """No token header → 401 even with valid body. Confirms the wire
    contract enforces token-auth before reaching the handler."""
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/investment-reports/hermes/context",
            json={"snapshot_bundle_uuid": str(_seeded_bundle.bundle_uuid)},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------


def test_fixture_files_exist_and_are_well_formed() -> None:
    """Belt-and-braces: the smoke test depends on the fixture files
    being valid JSON with the expected placeholder shape so the round
    trip itself doesn't double as fixture-syntax validation."""
    for name in (
        "stage_artifacts_request.json",
        "symbol_reports_request.json",
        "dimension_reports_request.json",
        "composition_request.json",
        "stage_artifacts_request_us.json",
        "symbol_reports_request_us.json",
        "dimension_reports_request_us.json",
        "composition_request_us.json",
    ):
        path = _FIXTURE_DIR / name
        assert path.exists(), f"missing fixture: {path}"
        text = path.read_text(encoding="utf-8")
        parsed = json.loads(text)
        assert "_comment" in parsed, f"{name}: missing operator-facing _comment"
        # Both placeholders must appear in each fixture.
        assert re.search(r"\{\{snapshot_bundle_uuid\}\}", text), (
            f"{name}: missing {{snapshot_bundle_uuid}} placeholder"
        )
        assert re.search(r"\{\{run_uuid\}\}", text), (
            f"{name}: missing {{run_uuid}} placeholder"
        )

    # The composition fixtures additionally carry the report-UUID placeholders
    # (ROB-309) substituted from the symbol-/dimension-reports responses.
    for name in ("composition_request.json", "composition_request_us.json"):
        text = (_FIXTURE_DIR / name).read_text(encoding="utf-8")
        assert re.search(r"\{\{symbol_report_uuid\}\}", text), (
            f"{name}: missing {{symbol_report_uuid}} placeholder"
        )
        assert re.search(r"\{\{dimension_report_uuid\}\}", text), (
            f"{name}: missing {{dimension_report_uuid}} placeholder"
        )


# ---------------------------------------------------------------------------
# ROB-287 follow-up — US narrow smoke
#
# Hermes-first contract round-trip against a ``market="us"`` /
# ``account_scope="alpaca_paper"`` snapshot bundle. Narrow scope: this
# verifies the four Hermes endpoints accept and persist a US-shaped
# payload + produce a draft InvestmentReport linked to the US bundle.
# The legacy ``ReportGenerationRequest`` (snapshot_backed generator)
# still does NOT accept ``market="us"`` — that path is intentionally
# out of scope and the runbook calls it out.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roundtrip_us_narrow_smoke(db_session, _enabled) -> None:
    """US narrow smoke — Hermes contract on ``market='us'`` /
    ``account_scope='alpaca_paper'``.

    Asserts:
    * Context export accepts the US bundle.
    * Stage-artifacts ingest persists 5 rows under one ``run_uuid``.
    * Composition ingest persists a **draft** InvestmentReport linked
      to the US bundle, with cited stage artifact UUIDs threaded into
      ``report_metadata.hermes_composition``.
    * Composition ingest auto-finalises the stage run (§D4) — same
      contract as KR.
    * Status MUST stay ``draft`` (the smoke does not produce
      published reports — operator review is required).
    """
    bundle = await _seed_bundle(db_session, market="us", account_scope="alpaca_paper")
    run_uuid = uuid.uuid4()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        # 1. context
        ctx_resp = await client.post(
            "/trading/api/investment-reports/hermes/context",
            headers=_auth_headers(),
            json={"snapshot_bundle_uuid": str(bundle.bundle_uuid)},
        )
        assert ctx_resp.status_code == 200, ctx_resp.text
        ctx_body = ctx_resp.json()
        assert ctx_body["market"] == "us"
        assert ctx_body["account_scope"] == "alpaca_paper"

        # 2. stage-artifacts ingest (US fixture)
        stage_payload = _substitute_placeholders(
            _load_fixture("stage_artifacts_request_us.json"),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle.bundle_uuid,
        )
        stage_resp = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts",
            headers=_auth_headers(),
            json=stage_payload,
        )
        assert stage_resp.status_code == 200, stage_resp.text
        stage_body = stage_resp.json()
        assert stage_body["success"] is True
        assert stage_body["run_uuid"] == str(run_uuid)
        assert stage_body["run_status"] == "running"
        assert len(stage_body["artifacts"]) == 5
        assert all(not r["idempotent_existing"] for r in stage_body["artifacts"])

        # 3. symbol-reports ingest (US fixture, ROB-301)
        symbol_payload = _substitute_placeholders(
            _load_fixture("symbol_reports_request_us.json"),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle.bundle_uuid,
        )
        sym_resp = await client.post(
            "/trading/api/investment-reports/hermes/symbol-reports",
            headers=_auth_headers(),
            json=symbol_payload,
        )
        assert sym_resp.status_code == 200, sym_resp.text
        sym_body = sym_resp.json()
        assert len(sym_body["symbol_reports"]) == 2
        symbol_report_uuid = sym_body["symbol_reports"][0]["symbol_report_uuid"]

        # 4. dimension-reports ingest (US fixture, ROB-306)
        dimension_payload = _substitute_placeholders(
            _load_fixture("dimension_reports_request_us.json"),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle.bundle_uuid,
        )
        dim_resp = await client.post(
            "/trading/api/investment-reports/hermes/dimension-reports",
            headers=_auth_headers(),
            json=dimension_payload,
        )
        assert dim_resp.status_code == 200, dim_resp.text
        dim_body = dim_resp.json()
        assert len(dim_body["dimension_reports"]) == 2
        market_dim = next(
            d for d in dim_body["dimension_reports"] if d["dimension"] == "market"
        )
        dimension_report_uuid = market_dim["dimension_report_uuid"]

        # 5. context re-pull carries both report families (US shape)
        ctx2_resp = await client.post(
            "/trading/api/investment-reports/hermes/context",
            headers=_auth_headers(),
            json={"snapshot_bundle_uuid": str(bundle.bundle_uuid)},
        )
        assert ctx2_resp.status_code == 200, ctx2_resp.text
        ctx2_body = ctx2_resp.json()
        assert len(ctx2_body["dimension_reports"]) == 2
        assert len(ctx2_body["symbol_intermediate_reports"]) == 2

        # 6. composition ingest (US fixture) — keep status=draft and make
        # generator_version unique so the shared db_session doesn't reuse
        # an existing report row from a sibling test. Threads the captured
        # symbol/dimension report UUIDs into the payload (ROB-308/ROB-301).
        composition_payload = _make_unique_for_test_db(
            _substitute_placeholders(
                _load_fixture("composition_request_us.json"),
                run_uuid=run_uuid,
                snapshot_bundle_uuid=bundle.bundle_uuid,
                symbol_report_uuid=symbol_report_uuid,
                dimension_report_uuid=dimension_report_uuid,
            ),
            run_uuid=run_uuid,
        )
        assert composition_payload["status"] == "draft", (
            "US narrow smoke must NOT publish — status MUST stay 'draft'"
        )
        assert composition_payload["market"] == "us"
        assert composition_payload["account_scope"] == "alpaca_paper"

        comp_resp = await client.post(
            "/trading/api/investment-reports/hermes/composition",
            headers=_auth_headers(),
            json=composition_payload,
        )
        assert comp_resp.status_code == 200, comp_resp.text
        comp_body = comp_resp.json()
        assert comp_body["success"] is True
        assert comp_body["status"] == "draft", (
            "InvestmentReport row MUST be 'draft' for the US narrow smoke."
        )
        assert comp_body["items_count"] == 4

        # --- DB-level assertions ---
        from sqlalchemy import select

        run_row = await db_session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )
        assert run_row is not None
        assert run_row.market == "us"
        assert run_row.account_scope == "alpaca_paper"
        assert run_row.status == "completed", (
            "§D4: composition ingest must auto-finalize the matching stage run."
        )
        assert run_row.completed_at is not None
        # Run is linked to the same US bundle.
        assert run_row.snapshot_bundle_uuid == bundle.bundle_uuid

        artifact_rows = list(
            (
                await db_session.scalars(
                    select(InvestmentStageArtifact).where(
                        InvestmentStageArtifact.run_uuid == run_uuid
                    )
                )
            ).all()
        )
        assert len(artifact_rows) == 5
        stage_types = {a.stage_type for a in artifact_rows}
        assert stage_types == {
            "market",
            "news",
            "portfolio_journal",
            "bull_reducer",
            "risk_review",
        }

        # InvestmentReport row is linked to the same US bundle and is draft.
        report_uuid = uuid.UUID(comp_body["report_uuid"])
        report_row = await db_session.scalar(
            select(InvestmentReport).where(InvestmentReport.report_uuid == report_uuid)
        )
        assert report_row is not None
        assert report_row.snapshot_bundle_uuid == bundle.bundle_uuid, (
            "InvestmentReport.snapshot_bundle_uuid must match the seeded US bundle."
        )
        assert report_row.status == "draft", (
            "Persisted InvestmentReport MUST stay 'draft' — published reports "
            "are operator-gated and out of this smoke's scope."
        )
        assert report_row.market == "us"
        assert report_row.account_scope == "alpaca_paper"
        hermes_meta = report_row.report_metadata.get("hermes_composition", {})
        assert hermes_meta.get("hermes_run_id") == "hermes-smoke-us-001"
        # ROB-308/ROB-301 — consumed report UUIDs threaded into metadata.
        assert report_row.report_metadata.get("symbol_intermediate_report_uuids") == [
            symbol_report_uuid
        ]
        assert report_row.report_metadata.get("dimension_report_uuids") == [
            dimension_report_uuid
        ]

        # Read surface: the final bundle classifies the new_buy_candidate item.
        read_app = _build_read_app(db_session)
        async with AsyncClient(
            transport=ASGITransport(app=read_app), base_url="https://test"
        ) as read_client:
            bundle_resp = await read_client.get(
                f"/trading/api/investment-reports/{report_uuid}"
            )
            assert bundle_resp.status_code == 200, bundle_resp.text
            rollup = bundle_resp.json()["decision_rollup"]
            assert len(rollup["new_candidate"]) == 1
            assert rollup["new_candidate"][0]["decision_bucket"] == "new_buy_candidate"


def test_us_fixtures_pin_alpaca_paper_and_draft_and_us_symbols() -> None:
    """Lock the US fixture invariants in code so a careless edit
    can't silently broaden the smoke into broker-touching territory.

    Specifically:
    * ``market`` is ``'us'`` on both fixtures and the run envelope.
    * ``account_scope`` is ``'alpaca_paper'`` — Alpaca paper is the
      only non-prod broker scope auto_trader supports today, and it
      is read-only with respect to the Hermes path (no order
      submission is reachable from any Hermes endpoint).
    * The composition fixture ``status`` is ``'draft'`` — the smoke
      must never publish.
    * Symbol-bearing items reference ``AAPL`` and ``MSFT`` only —
      stress-test ticker, not stress-test universe.
    * No item has ``operation`` ∈ ``{create, modify}`` — Hermes
      contract rejects those, but the fixture lock makes the
      intent explicit.
    """
    stage_payload = _load_fixture("stage_artifacts_request_us.json")
    assert stage_payload["run_envelope"]["market"] == "us"
    assert stage_payload["run_envelope"]["account_scope"] == "alpaca_paper"

    composition_payload = _load_fixture("composition_request_us.json")
    assert composition_payload["market"] == "us"
    assert composition_payload["account_scope"] == "alpaca_paper"
    assert composition_payload["status"] == "draft"

    items = composition_payload["composition"]["items"]
    symbols = {it.get("symbol") for it in items if it.get("symbol")}
    assert symbols.issubset({"AAPL", "MSFT"}), (
        f"US smoke ticker scope locked to AAPL/MSFT; got {symbols!r}"
    )
    for it in items:
        assert it["operation"] in {"review", "cancel", "keep"}, (
            f"US smoke item {it['client_item_key']!r} has operation="
            f"{it['operation']!r}; advisory-only contract forbids "
            "create/modify/place/cancel-order verbs"
        )
        assert it["apply_policy"] == "requires_user_approval"
