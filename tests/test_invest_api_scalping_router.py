"""ROB-315 Phase 2 — router tests for read/review /invest/api/scalping.

The service is stubbed (its DB behavior is covered by the Phase 1 service
tests); these assert request parsing, response shape, status codes, and the
broker/order/scheduler import boundary.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.scalping_reviews import ScalpingDailyReview, ScalpingReviewAction
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_scalping import get_scalping_review_service
from app.routers.invest_scalping import router as scalping_router
from app.services.scalping_reviews.service import ScalpingReviewError

_NOW = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.UTC)


def _make_review(review_id: int, **kw) -> ScalpingDailyReview:
    base = {
        "id": review_id,
        "review_date": dt.date(2026, 5, 25),
        "product": "usdm_futures",
        "account_scope": "binance_demo",
        "session_tag": "",
        "trade_count": 2,
        "win_count": 1,
        "loss_count": 1,
        "anomaly_count": 0,
        "gross_pnl_usdt": Decimal("0.0"),
        "net_pnl_usdt": Decimal("-0.2"),
        "net_return_bps": Decimal("-10"),
        "avg_slippage_bps": Decimal("3"),
        "avg_spread_bps": None,
        "avg_mae_bps": Decimal("-10"),
        "avg_mfe_bps": Decimal("40"),
        "avg_holding_seconds": 15,
        "exit_reason_counts": {"take_profit": 1, "stop_loss": 1},
        "observation": None,
        "root_cause": None,
        "improvement": None,
        "next_run_plan": None,
        "decision": "review",
        "status": "draft",
        "source_payload": {"row_count": 2},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kw)
    return ScalpingDailyReview(**base)


def _make_action(action_id: int, review_id: int, **kw) -> ScalpingReviewAction:
    base = {
        "id": action_id,
        "review_id": review_id,
        "action_type": "parameter_change",
        "title": "widen TP",
        "rationale": None,
        "target_component": None,
        "proposed_change": None,
        "expected_effect": None,
        "status": "open",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kw)
    return ScalpingReviewAction(**base)


def _make_analytics(**kw):
    from app.models.scalp_trade_analytics import ScalpTradeAnalytics

    base = {
        "id": 1,
        "open_client_order_id": "o-1",
        "instrument_id": 1,
        "product": "usdm_futures",
        "symbol": "XRPUSDT",
        "side": "BUY",
        "qty": Decimal("1"),
        "entry_price": Decimal("100"),
        "exit_price": Decimal("101"),
        "entry_slippage_bps": Decimal("2"),
        "mae_bps": Decimal("-10"),
        "mfe_bps": Decimal("40"),
        "net_pnl_usdt": Decimal("0.9"),
        "holding_seconds": 12,
        "exit_reason": "take_profit",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kw)
    return ScalpTradeAnalytics(**base)


class _StubService:
    async def list_analytics(self, *, review_date, product):
        return [_make_analytics(), _make_analytics(id=2, entry_price=None)]

    async def list_reviews(self, *, review_date=None, product=None):
        return [_make_review(1)]

    async def get(self, review_id):
        return _make_review(review_id) if review_id == 1 else None

    async def list_actions(self, review_id):
        return [_make_action(10, review_id)]

    async def build_draft(self, *, review_date, product, session_tag, now):
        return _make_review(
            2, review_date=review_date, product=product, session_tag=session_tag
        )

    async def update_review(self, review_id, *, now, **fields):
        if review_id != 1:
            return None
        r = _make_review(1)
        for k, v in fields.items():
            setattr(r, k, v)
        return r

    async def add_action(self, review_id, *, action_type, title, now, **kw):
        return _make_action(11, review_id, action_type=action_type, title=title)

    async def update_action(self, action_id, *, now, **fields):
        if action_id != 10:
            return None
        a = _make_action(10, 1)
        for k, v in fields.items():
            setattr(a, k, v)
        return a


def _client(service=None) -> TestClient:
    app = FastAPI()
    app.include_router(scalping_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 7}
    )()
    app.dependency_overrides[get_scalping_review_service] = lambda: (
        service or _StubService()
    )
    return TestClient(app)


def test_list_reviews_serializes_metrics() -> None:
    resp = _client().get(
        "/invest/api/scalping/reviews?date=2026-05-25&product=usdm_futures"
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    m = items[0]["metrics"]
    assert m["tradeCount"] == 2
    assert m["netPnlUsdt"] == "-0.2"  # Decimal serialized as string
    assert m["avgSpreadBps"] is None  # n/a, not 0
    assert m["exitReasonCounts"] == {"take_profit": 1, "stop_loss": 1}


def test_list_analytics_marks_anomaly_rows() -> None:
    resp = _client().get(
        "/invest/api/scalping/analytics?date=2026-05-25&product=usdm_futures"
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["isAnomaly"] is False
    assert items[0]["entryPrice"] == "100"
    assert items[1]["isAnomaly"] is True  # no derivable fill price
    assert items[1]["entryPrice"] is None


def test_get_review_includes_actions() -> None:
    resp = _client().get("/invest/api/scalping/reviews/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["review"]["id"] == 1
    assert body["actions"][0]["actionType"] == "parameter_change"


def test_get_missing_review_is_404() -> None:
    assert _client().get("/invest/api/scalping/reviews/999").status_code == 404


def test_build_draft_echoes_request() -> None:
    resp = _client().post(
        "/invest/api/scalping/reviews/draft",
        json={"review_date": "2026-05-25", "product": "usdm_futures"},
    )
    assert resp.status_code == 200
    assert resp.json()["review"]["product"] == "usdm_futures"


def test_patch_review_updates_operator_fields() -> None:
    resp = _client().patch(
        "/invest/api/scalping/reviews/1",
        json={"decision": "adjust", "observation": "spread widened"},
    )
    assert resp.status_code == 200
    review = resp.json()["review"]
    assert review["decision"] == "adjust"
    assert review["observation"] == "spread widened"


def test_patch_review_rejects_bad_decision() -> None:
    resp = _client().patch("/invest/api/scalping/reviews/1", json={"decision": "bogus"})
    assert resp.status_code == 422  # pydantic Literal validation


def test_create_action_on_missing_review_is_404() -> None:
    resp = _client().post(
        "/invest/api/scalping/reviews/999/actions",
        json={"action_type": "investigate", "title": "x"},
    )
    assert resp.status_code == 404


def test_patch_action_status() -> None:
    resp = _client().patch(
        "/invest/api/scalping/actions/10", json={"status": "applied"}
    )
    assert resp.status_code == 200
    assert resp.json()["action"]["status"] == "applied"


def test_service_error_maps_to_422() -> None:
    class _Boom(_StubService):
        async def build_draft(self, **kw):
            raise ScalpingReviewError("non-demo scope")

    resp = _client(_Boom()).post(
        "/invest/api/scalping/reviews/draft",
        json={"review_date": "2026-05-25", "product": "usdm_futures"},
    )
    assert resp.status_code == 422


_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "brokers",
    "scheduler",
    "executor",
    "execution_client",
    "order_intent",
    "demo_scalping",
    "kis_trading",
    "kis_holdings",
    "upbit",
    "alpaca",
    "binance",
)


def test_router_imports_no_broker_order_scheduler_modules() -> None:
    """The review surface must never import a broker/order/scheduler/market-data
    mutation module (ROB-315 safety boundary)."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "routers"
        / "invest_scalping.py"
    ).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    offenders = [
        mod for mod in imported for bad in _FORBIDDEN_IMPORT_SUBSTRINGS if bad in mod
    ]
    assert not offenders, f"forbidden imports in invest_scalping router: {offenders}"
