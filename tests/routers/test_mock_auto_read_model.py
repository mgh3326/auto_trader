"""ROB-1272 (J7) — read-only router for the cross-lane mock/paper/demo model.

The router is exercised with injected read-only ports; no database session is
opened, no broker is called, and no mutating verb is registered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.mock_auto_read_model import EvidenceClass
from app.services.mock_auto_read_model import (
    RawEvidenceRecord,
    SourceReadResult,
)

AS_OF = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)


class _StaticPort:
    def __init__(self, records: tuple[RawEvidenceRecord, ...]) -> None:
        self._records = records

    async def read(self, *, lane_id: str, source_id: str) -> SourceReadResult:
        del lane_id
        return SourceReadResult(source_id=source_id, records=self._records)


def _record(**overrides) -> RawEvidenceRecord:
    base = {
        "source_id": "kis_mock_ledger",
        "evidence_class": EvidenceClass.DB_LEDGER,
        "native_key": "kis_mock_ledger:1",
        "as_of": AS_OF,
        "native_status": "accepted",
        "venue_basis": "kis_mock_ledger",
        "observed_at": AS_OF,
        "decision_intent_id": "intent-1",
        "execution_plan_id": "plan-1",
        "order_attempt_id": "attempt-1",
        "cycle_id": "cycle-1",
        "idempotency_key": "idem-1",
        "broker_ack": True,
    }
    base.update(overrides)
    return RawEvidenceRecord(**base)  # type: ignore[arg-type]


def _make_client(ports=None) -> TestClient:
    from app.core.db import get_db
    from app.routers import mock_auto_read_model as router_module
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: None

    resolved = ports or {}
    app.dependency_overrides.setdefault(get_db, lambda: None)

    original = router_module.build_default_ports
    router_module.build_default_ports = lambda **_kwargs: resolved  # type: ignore[assignment]
    client = TestClient(app)
    client.__dict__["_restore_ports"] = lambda: setattr(
        router_module, "build_default_ports", original
    )
    return client


@pytest.mark.unit
def test_coverage_endpoint_returns_all_twelve_lanes():
    client = _make_client()
    try:
        response = client.get("/trading/api/mock-auto/read-model/coverage")
        assert response.status_code == 200
        payload = response.json()
        assert payload["schema_version"] == "mock-auto-read-model-v1"
        assert len(payload["coverage_rows"]) == 12
        assert payload["notes"]["read_only"] is True
    finally:
        client.__dict__["_restore_ports"]()


@pytest.mark.unit
def test_coverage_response_separates_the_four_collections():
    client = _make_client()
    try:
        payload = client.get("/trading/api/mock-auto/read-model/coverage").json()
        for key in (
            "coverage_rows",
            "lifecycle_rows",
            "anomalies",
            "anomaly_counts",
            "holds",
            "hold_counts",
            "unlinked_evidence",
            "unlinked_evidence_counts",
        ):
            assert key in payload, key
    finally:
        client.__dict__["_restore_ports"]()


@pytest.mark.unit
def test_observations_endpoint_fans_out_by_decision_intent_id():
    ports = {
        "kis_mock_ledger": _StaticPort((_record(),)),
        "alpaca_paper_ledger": _StaticPort(
            (
                _record(
                    source_id="alpaca_paper_ledger",
                    native_key="alpaca_paper_ledger:1",
                    venue_basis="alpaca_paper_ledger",
                ),
            )
        ),
    }
    client = _make_client(ports)
    try:
        rows = client.get(
            "/trading/api/mock-auto/read-model/observations",
            params={"decision_intent_id": "intent-1"},
        ).json()
        assert {row["lane_id"] for row in rows} >= {
            "kr.kis.mock",
            "us.alpaca.paper.default",
        }
        assert {tuple(sorted(row)) for row in rows} == {tuple(sorted(rows[0]))}
        for row in rows:
            assert row["decision_intent_id"] == "intent-1"
            assert row["execution_plan_id"] == "plan-1"
            assert row["order_attempt_id"] == "attempt-1"
    finally:
        client.__dict__["_restore_ports"]()


@pytest.mark.unit
def test_observations_endpoint_filters_by_lane_id():
    ports = {"kis_mock_ledger": _StaticPort((_record(),))}
    client = _make_client(ports)
    try:
        rows = client.get(
            "/trading/api/mock-auto/read-model/observations",
            params={"lane_id": "kr.kis.mock"},
        ).json()
        assert rows
        assert {row["lane_id"] for row in rows} == {"kr.kis.mock"}
        empty = client.get(
            "/trading/api/mock-auto/read-model/observations",
            params={"lane_id": "crypto.upbit.shadow"},
        ).json()
        assert empty == []
    finally:
        client.__dict__["_restore_ports"]()


@pytest.mark.unit
def test_partial_fill_is_not_reported_as_a_full_fill():
    ports = {
        "kis_mock_ledger": _StaticPort(
            (
                _record(
                    broker_ack=False,
                    fill_evidence=True,
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("4"),
                ),
            )
        )
    }
    client = _make_client(ports)
    try:
        rows = client.get("/trading/api/mock-auto/read-model/observations").json()
        assert len(rows) == 1
        assert rows[0]["stage"] == "filled"
        assert rows[0]["partial_fill"] is True
        assert rows[0]["remaining_quantity"] == "4"
    finally:
        client.__dict__["_restore_ports"]()


@pytest.mark.unit
def test_bindings_endpoint_exposes_the_stamped_sources_without_secrets():
    client = _make_client()
    try:
        bindings = client.get("/trading/api/mock-auto/read-model/bindings").json()
        assert {row["source_id"] for row in bindings} == {
            "alpaca_paper_ledger",
            "binance_demo_ledger",
            "kis_mock_ledger",
            "kiwoom_kr_native_readback",
            "kiwoom_kr_ordering_events",
            "kiwoom_kr_own_orders",
        }
        for row in bindings:
            assert row["redaction_contract"]
            assert "://" not in row["logical_locator"]
            assert len(row["predecessor_verifier_report_sha256"]) == 64
    finally:
        client.__dict__["_restore_ports"]()


@pytest.mark.unit
def test_mutating_verbs_are_not_registered():
    from app.routers import mock_auto_read_model as router_module

    for route in router_module.router.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}, route.path


@pytest.mark.unit
def test_router_is_registered_on_the_application_once():
    from app.main import create_app

    app = create_app()
    paths = [
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/trading/api/mock-auto/read-model")
    ]
    assert sorted(paths) == [
        "/trading/api/mock-auto/read-model/bindings",
        "/trading/api/mock-auto/read-model/coverage",
        "/trading/api/mock-auto/read-model/observations",
    ]
