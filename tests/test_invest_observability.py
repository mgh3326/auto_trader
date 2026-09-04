"""Regression tests for the first invest latency instrumentation pass."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

import app.monitoring.sentry as sentry_module
from app.middleware.invest_timing import (
    _server_timing_header,
    _span_metrics,
)
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import _rum_rate_gate, get_invest_home_service
from app.routers.invest_api import router as invest_router
from app.schemas.invest_home import HomeSummary, InvestHomeResponse


class _TransactionTransport(Transport):
    def __init__(self, options):
        super().__init__(options)
        self.transactions: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "transaction":
                self.transactions.append(item.payload.json)


def test_real_app_invest_transactions_name_authenticated_and_pre_auth_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete middleware stack must never emit an auth-class transaction."""
    import app.main as app_main
    from app.middleware.auth import AuthMiddleware

    transport = _TransactionTransport({})

    def init_test_sentry(**_: object) -> bool:
        sentry_sdk.init(
            dsn="https://public@example.ingest.sentry.io/1",
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            traces_sample_rate=1.0,
            transport=transport,
            before_send_transaction=sentry_module._before_send_transaction,
            default_integrations=False,
        )
        return True

    class HomeService:
        async def get_home(self, **_: object) -> InvestHomeResponse:
            with sentry_sdk.start_span(op="db.query", name="load home"):
                pass
            with sentry_sdk.start_span(op="http.client", name="quote provider"):
                pass
            return InvestHomeResponse(
                homeSummary=HomeSummary(
                    includedSources=[], excludedSources=[], totalValueKrw=0
                ),
                accounts=[],
                holdings=[],
                groupedHoldings=[],
            )

    async def authenticated_user(_: object) -> SimpleNamespace:
        return SimpleNamespace(id=7)

    async def unauthenticated_user(_: object) -> None:
        return None

    monkeypatch.setattr(app_main, "init_sentry", init_test_sentry)
    monkeypatch.setattr(AuthMiddleware, "_load_user", staticmethod(authenticated_user))
    app = app_main.create_app()
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    app.dependency_overrides[get_invest_home_service] = HomeService

    client = TestClient(app)
    response = client.get("/invest/api/home")

    sentry_sdk.flush()
    assert response.status_code == 200
    assert "total;dur=" in response.headers["server-timing"]
    assert "db;dur=" in response.headers["server-timing"]
    assert "ext;dur=" in response.headers["server-timing"]
    transaction = next(
        item
        for item in transport.transactions
        if item["transaction"] == "GET /invest/api/home"
    )
    assert transaction["transaction"] == "GET /invest/api/home"
    assert transaction["transaction_info"]["source"] == "route"
    assert {span["op"] for span in transaction["spans"]} >= {
        "db.query",
        "http.client",
    }

    transport.transactions.clear()
    monkeypatch.setattr(
        AuthMiddleware, "_load_user", staticmethod(unauthenticated_user)
    )
    unauthorized = client.get("/invest/api/home")
    sentry_sdk.flush()

    assert unauthorized.status_code == 401
    pre_auth = next(item for item in transport.transactions if item["transaction"])
    assert pre_auth["transaction"] == "GET /invest/api/* (pre-auth)"
    assert pre_auth["transaction_info"]["source"] == "custom"


def test_server_timing_sums_db_and_each_external_span() -> None:
    started = datetime.now(UTC)

    def span(op: str, ms: float) -> SimpleNamespace:
        return SimpleNamespace(
            op=op,
            start_timestamp=started,
            timestamp=started + timedelta(milliseconds=ms),
            _data={},
            _tags={},
        )

    transaction = SimpleNamespace(
        _span_recorder=SimpleNamespace(
            spans=[
                span("db.query", 11),
                span("http.client", 19),
                span("http.client", 7),
            ]
        )
    )
    db_ms, ext_ms, cache = _span_metrics(
        SimpleNamespace(containing_transaction=transaction)
    )

    assert db_ms == pytest.approx(11)
    assert ext_ms == pytest.approx(26)
    assert cache is None
    assert (
        _server_timing_header(total_ms=42, db_ms=db_ms, ext_ms=ext_ms, cache=cache)
        == "total;dur=42.0, db;dur=11.0, ext;dur=26.0"
    )


@pytest.fixture
def rum_client() -> TestClient:
    _rum_rate_gate._last_seen.clear()
    app = FastAPI()
    app.include_router(invest_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    return TestClient(app)


def test_rum_requires_small_body_and_one_request_per_five_seconds(
    rum_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Mock()
    monkeypatch.setattr("app.routers.invest_api.sentry_sdk.capture_message", capture)
    payload = {
        "route": "/invest",
        "n_requests": 3,
        "wall_ms": 104.5,
        "slowest": "/invest/api/home",
    }

    accepted = rum_client.post("/invest/api/rum", json=payload)
    rate_limited = rum_client.post("/invest/api/rum", json=payload)
    too_large = rum_client.post("/invest/api/rum", content=b"x" * 2_049)

    assert accepted.status_code == 204
    assert rate_limited.status_code == 429
    assert too_large.status_code == 413
    capture.assert_called_once_with("invest.rum", level="info")


def test_rum_rejects_untemplated_identifiers_before_sentry_tags(
    rum_client: TestClient,
) -> None:
    raw = {
        "route": "/invest/reports/550e8400-e29b-41d4-a716-446655440000",
        "n_requests": 1,
        "wall_ms": 10,
        "slowest": "/invest/api/symbols/005930/quote",
    }
    normalized = {
        **raw,
        "route": "/invest/reports/:id",
        "slowest": "/invest/api/symbols/:symbol/quote",
    }

    assert rum_client.post("/invest/api/rum", json=raw).status_code == 422
    assert rum_client.post("/invest/api/rum", json=normalized).status_code == 204
