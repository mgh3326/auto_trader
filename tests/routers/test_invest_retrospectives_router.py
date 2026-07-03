from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client(
    monkeypatch, *, list_result=None, na_result=None, aggregate_result=None
):
    from app.core.db import get_db
    from app.routers import invest_retrospectives
    from app.routers.dependencies import get_authenticated_user

    calls: dict = {}

    async def _fake_list(db, **kwargs):
        calls["list"] = kwargs
        return list_result or {"entries": [], "summary": {"count": 0, "total": 0}}

    async def _fake_na(db, **kwargs):
        calls["na"] = kwargs
        return na_result or {"items": [], "count": 0, "scan_limit": 200}

    async def _fake_aggregate(db, **kwargs):
        calls["aggregate"] = kwargs
        return aggregate_result or {
            "group_by": kwargs.get("group_by", "strategy"),
            "groups": [],
            "excluded_no_fill_evidence": 0,
        }

    monkeypatch.setattr(
        invest_retrospectives.retro_svc, "get_retrospectives", _fake_list
    )
    monkeypatch.setattr(
        invest_retrospectives.retro_svc, "get_open_next_actions", _fake_na
    )
    monkeypatch.setattr(
        invest_retrospectives.retro_svc,
        "build_retrospective_aggregate",
        _fake_aggregate,
    )

    app = FastAPI()
    app.include_router(invest_retrospectives.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_list_defaults_all_market(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives")
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "all"
    assert body["total"] == 0
    # market="all" -> service market filter omitted (None)
    assert calls["list"]["market"] is None


@pytest.mark.unit
def test_list_forwards_filters_and_pagination(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/retrospectives"
        "?market=kr&trigger_type=fill&root_cause_class=analysis&limit=10&offset=20"
    )
    assert r.status_code == 200
    assert calls["list"]["market"] == "kr"
    assert calls["list"]["trigger_type"] == "fill"
    assert calls["list"]["root_cause_class"] == "analysis"
    assert calls["list"]["limit"] == 10
    assert calls["list"]["offset"] == 20


@pytest.mark.unit
def test_list_normalizes_us_symbol(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives?market=us&symbol=BRK-B")
    assert r.status_code == 200
    assert calls["list"]["symbol"] == "BRK.B"  # to_db_symbol applied


@pytest.mark.unit
def test_list_rejects_invalid_enums(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get("/trading/api/invest/retrospectives?trigger_type=bogus").status_code
        == 422
    )
    assert (
        client.get(
            "/trading/api/invest/retrospectives?root_cause_class=bogus"
        ).status_code
        == 422
    )
    assert (
        client.get("/trading/api/invest/retrospectives?market=paper").status_code == 422
    )


@pytest.mark.unit
def test_list_maps_entries_to_items(monkeypatch):
    entry = {
        "id": 1,
        "correlation_id": "c",
        "symbol": "005930",
        "market": "kr",
        "instrument_type": "equity_kr",
        "trigger_type": "fill",
        "realized_pnl": 1000.0,
        "next_actions": [{"action": "x"}],
        "created_at": "2026-07-01T00:00:00+00:00",
        "outcome": "win",
        "extra_ignored_field": "dropped",
    }
    client, _ = _make_client(
        monkeypatch,
        list_result={"entries": [entry], "summary": {"count": 1, "total": 5}},
    )
    r = client.get("/trading/api/invest/retrospectives")
    body = r.json()
    assert body["count"] == 1
    assert body["total"] == 5
    assert body["items"][0]["symbol"] == "005930"
    assert "extra_ignored_field" not in body["items"][0]


@pytest.mark.unit
def test_next_actions_endpoint(monkeypatch):
    item = {
        "action": "재검토",
        "status": "open",
        "symbol": "005930",
        "market": "kr",
        "retro_id": 1,
        "correlation_id": "c",
        "trigger_type": "fill",
        "realized_pnl": None,
        "created_at": "2026-07-01T00:00:00+00:00",
        "owner": None,
        "issue_id": None,
        "due_kst_date": None,
    }
    client, calls = _make_client(
        monkeypatch,
        na_result={"items": [item], "count": 1, "scan_limit": 200},
    )
    r = client.get(
        "/trading/api/invest/retrospectives/next-actions?market=kr&symbol=005930"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["scan_limit"] == 200
    assert body["items"][0]["action"] == "재검토"
    assert calls["na"]["market"] == "kr"
    assert calls["na"]["symbol"] == "005930"


@pytest.mark.unit
def test_next_actions_status_csv_narrows(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/retrospectives/next-actions?status=open,in_progress"
    )
    assert r.status_code == 200
    assert calls["na"]["statuses"] == frozenset({"open", "in_progress"})


@pytest.mark.unit
def test_list_forwards_new_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/retrospectives"
        "?outcome_filter=win&q=005&kst_date_from=2026-07-01&kst_date_to=2026-07-04"
    )
    assert r.status_code == 200
    assert calls["list"]["outcome_filter"] == "win"
    assert calls["list"]["symbol_search"] == "005"
    assert calls["list"]["kst_date_from"] == "2026-07-01"
    assert calls["list"]["kst_date_to"] == "2026-07-04"
    body = r.json()
    assert body["outcome_filter"] == "win"
    assert body["q"] == "005"
    assert body["kst_date_from"] == "2026-07-01"
    assert body["kst_date_to"] == "2026-07-04"


@pytest.mark.unit
def test_list_rejects_invalid_outcome_filter(monkeypatch):
    client, _ = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives?outcome_filter=bogus")
    assert r.status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize("param", ["kst_date_from", "kst_date_to"])
def test_list_rejects_invalid_date_format(monkeypatch, param):
    client, _ = _make_client(monkeypatch)
    r = client.get(f"/trading/api/invest/retrospectives?{param}=2026/07/04")
    assert r.status_code == 422


@pytest.mark.unit
def test_scoreboard_defaults_and_totals_rollup(monkeypatch):
    groups = [
        {
            "group": "A",
            "sample_size": 5,
            "wins": 3,
            "misses": 2,
            "win_rate_pct": 60.0,
            "avg_pnl_pct": 1.2,
            "realized_pnl_sum": {"KRW": 100.0, "USD": 5.0},
            "fx_pnl_krw_sum": 10.0,
            "total_pnl_krw_sum": 110.0,
            "by_outcome": {"filled": 5},
            "by_trigger_type": {},
            "by_root_cause_class": {},
        },
        {
            "group": "B",
            "sample_size": 2,
            "wins": 1,
            "misses": 1,
            "win_rate_pct": 50.0,
            "avg_pnl_pct": -0.5,
            "realized_pnl_sum": {"KRW": 50.0},
            "fx_pnl_krw_sum": 0.0,
            "total_pnl_krw_sum": 50.0,
            "by_outcome": {"filled": 2},
            "by_trigger_type": {},
            "by_root_cause_class": {},
        },
    ]
    client, calls = _make_client(
        monkeypatch,
        aggregate_result={
            "group_by": "strategy",
            "groups": groups,
            "excluded_no_fill_evidence": 3,
        },
    )
    r = client.get("/trading/api/invest/retrospectives/scoreboard")
    assert r.status_code == 200
    body = r.json()
    assert body["group_by"] == "strategy"
    assert body["market"] == "all"
    assert calls["aggregate"]["market"] is None  # market="all" -> None
    assert calls["aggregate"]["group_by"] == "strategy"
    assert len(body["groups"]) == 2

    totals = body["totals"]
    assert totals["sample_size"] == 7
    assert totals["wins"] == 4
    assert totals["misses"] == 3
    assert totals["decided"] == 7
    assert totals["win_rate_pct"] == pytest.approx(4 / 7 * 100.0)
    assert totals["realized_pnl_sum"] == {"KRW": 150.0, "USD": 5.0}
    assert totals["fx_pnl_krw_sum"] == pytest.approx(10.0)
    assert totals["total_pnl_krw_sum"] == pytest.approx(160.0)
    assert totals["excluded_no_fill_evidence"] == 3


@pytest.mark.unit
def test_scoreboard_empty_groups_totals_are_zero_and_null_win_rate(monkeypatch):
    client, _ = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives/scoreboard")
    assert r.status_code == 200
    totals = r.json()["totals"]
    assert totals["sample_size"] == 0
    assert totals["decided"] == 0
    assert totals["win_rate_pct"] is None
    assert totals["realized_pnl_sum"] == {}


@pytest.mark.unit
def test_scoreboard_forwards_group_by_and_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/retrospectives/scoreboard"
        "?group_by=day&market=kr&account_mode=kis_live&strategy_key=A"
        "&kst_date_from=2026-07-01&kst_date_to=2026-07-04"
    )
    assert r.status_code == 200
    assert calls["aggregate"]["group_by"] == "day"
    assert calls["aggregate"]["market"] == "kr"
    assert calls["aggregate"]["account_mode"] == "kis_live"
    assert calls["aggregate"]["strategy_key"] == "A"
    assert calls["aggregate"]["kst_date_from"] == "2026-07-01"
    assert calls["aggregate"]["kst_date_to"] == "2026-07-04"
    body = r.json()
    assert body["market"] == "kr"
    assert body["kst_date_from"] == "2026-07-01"
    assert body["kst_date_to"] == "2026-07-04"


@pytest.mark.unit
def test_scoreboard_rejects_invalid_group_by(monkeypatch):
    client, _ = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives/scoreboard?group_by=bogus")
    assert r.status_code == 422


@pytest.mark.unit
def test_scoreboard_rejects_invalid_market(monkeypatch):
    client, _ = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives/scoreboard?market=paper")
    assert r.status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize("param", ["kst_date_from", "kst_date_to"])
def test_scoreboard_rejects_invalid_date_format(monkeypatch, param):
    client, _ = _make_client(monkeypatch)
    r = client.get(f"/trading/api/invest/retrospectives/scoreboard?{param}=07-04-2026")
    assert r.status_code == 422


@pytest.mark.unit
def test_scoreboard_requires_authentication():
    client = _make_unauth_client()
    r = client.get("/trading/api/invest/retrospectives/scoreboard")
    assert r.status_code == 401
    assert r.json()["detail"] == "로그인이 필요합니다."


def _make_unauth_client():
    """Client with the real get_authenticated_user (no override) -> 401 path."""
    from app.core.db import get_db
    from app.routers import invest_retrospectives

    app = FastAPI()
    app.include_router(invest_retrospectives.router)
    # Only stub get_db; leave get_authenticated_user real so the cookieless
    # request resolves to HTTP 401 (state.user absent, no session cookie).
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app)


@pytest.mark.unit
def test_list_requires_authentication():
    client = _make_unauth_client()
    r = client.get("/trading/api/invest/retrospectives")
    assert r.status_code == 401
    assert r.json()["detail"] == "로그인이 필요합니다."


@pytest.mark.unit
def test_next_actions_requires_authentication():
    client = _make_unauth_client()
    r = client.get("/trading/api/invest/retrospectives/next-actions")
    assert r.status_code == 401
    assert r.json()["detail"] == "로그인이 필요합니다."
