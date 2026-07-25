from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client(monkeypatch, *, calib=None, open_res=None, closed_res=None):
    from app.core.db import get_db
    from app.routers import invest_forecasts
    from app.routers.dependencies import get_authenticated_user

    calls: dict = {}

    async def _fake_calib(db, **kwargs):
        calls["calib"] = kwargs
        return calib or {"group_by": kwargs.get("group_by", "created_by"), "groups": []}

    async def _fake_open(db, **kwargs):
        calls["open"] = kwargs
        return open_res or {"entries": [], "summary": {"count": 0, "by_status": {}}}

    async def _fake_closed(db, **kwargs):
        calls["closed"] = kwargs
        return closed_res or {"entries": [], "summary": {"count": 0, "by_status": {}}}

    monkeypatch.setattr(
        invest_forecasts.fc_svc, "build_forecast_calibration_aggregate", _fake_calib
    )
    monkeypatch.setattr(invest_forecasts.fc_svc, "list_open_forecasts", _fake_open)
    monkeypatch.setattr(invest_forecasts.fc_svc, "list_closed_forecasts", _fake_closed)

    app = FastAPI()
    app.include_router(invest_forecasts.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_calibration_defaults(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/forecasts/calibration")
    assert r.status_code == 200
    body = r.json()
    assert body["group_by"] == "created_by"
    assert body["count"] == 0
    assert calls["calib"]["group_by"] == "created_by"


@pytest.mark.unit
def test_calibration_forwards_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/forecasts/calibration"
        "?group_by=model_label&created_by=hermes&symbol=AAPL"
        "&instrument_type=equity_us&days=30"
    )
    assert r.status_code == 200
    assert calls["calib"]["group_by"] == "model_label"
    assert calls["calib"]["created_by"] == "hermes"
    assert calls["calib"]["symbol"] == "AAPL"
    assert calls["calib"]["instrument_type"] == "equity_us"
    assert calls["calib"]["days"] == 30


@pytest.mark.unit
def test_calibration_maps_groups(monkeypatch):
    group = {
        "group": "hermes",
        "sample_size": 4,
        "hits": 3,
        "misses": 1,
        "hit_rate": 0.75,
        "avg_brier_score": 0.18,
        "avg_probability": 0.8,
        "calibration_gap": 0.05,
        "extra_dropped": "x",
    }
    client, _ = _make_client(
        monkeypatch, calib={"group_by": "created_by", "groups": [group]}
    )
    r = client.get("/trading/api/invest/forecasts/calibration")
    body = r.json()
    assert body["count"] == 1
    assert body["groups"][0]["group"] == "hermes"
    assert body["groups"][0]["hit_rate"] == 0.75
    assert "extra_dropped" not in body["groups"][0]


@pytest.mark.unit
def test_calibration_rejects_invalid_group_by(monkeypatch):
    client, _ = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/forecasts/calibration?group_by=bogus")
    assert r.status_code == 422


@pytest.mark.unit
def test_rejects_invalid_instrument_type(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get(
            "/trading/api/invest/forecasts/calibration?instrument_type=bogus"
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/trading/api/invest/forecasts/open?instrument_type=bogus"
        ).status_code
        == 422
    )


@pytest.mark.unit
def test_open_forwards_filters_and_maps(monkeypatch):
    entry = {
        "id": 7,
        "forecast_id": "11111111-1111-1111-1111-111111111111",
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "probability": 0.6,
        "review_date": "2026-07-10",
        "status": "open",
        "created_at": "2026-07-01T00:00:00+00:00",
        "extra_dropped": "x",
    }
    client, calls = _make_client(
        monkeypatch,
        open_res={
            "entries": [entry],
            "summary": {"count": 1, "by_status": {"open": 1}},
        },
    )
    r = client.get(
        "/trading/api/invest/forecasts/open?symbol=005930&created_by=hermes&limit=10"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "open"
    assert body["count"] == 1
    assert body["items"][0]["id"] == 7
    assert "extra_dropped" not in body["items"][0]
    assert calls["open"]["symbol"] == "005930"
    assert calls["open"]["created_by"] == "hermes"
    assert calls["open"]["limit"] == 10


@pytest.mark.unit
def test_closed_endpoint(monkeypatch):
    entry = {
        "id": 9,
        "forecast_id": "22222222-2222-2222-2222-222222222222",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
        "probability": 0.7,
        "review_date": "2026-06-20",
        "status": "closed",
        "outcome": True,
        "brier_score": 0.09,
        "resolved_at": "2026-06-21T00:00:00+00:00",
        "created_at": "2026-06-10T00:00:00+00:00",
    }
    client, calls = _make_client(
        monkeypatch,
        closed_res={
            "entries": [entry],
            "summary": {"count": 1, "by_status": {"closed": 1}},
        },
    )
    r = client.get("/trading/api/invest/forecasts/closed?instrument_type=equity_us")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "closed"
    assert body["count"] == 1
    assert body["items"][0]["outcome"] is True
    assert body["items"][0]["brier_score"] == 0.09
    assert calls["closed"]["instrument_type"] == "equity_us"
