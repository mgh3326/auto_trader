"""Integration tests for the generic sell alert pipeline.

Covers:
- sell_conditions_service DB CRUD
- Batch endpoint (GET /api/n8n/sell-signal/batch)
- Single-symbol endpoint DB default loading
- n8n workflow JSON structure validation
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.models.sell_condition import SellCondition
from app.routers.n8n import router
from app.schemas.n8n.sell_signal import (
    N8nSellSignalBatchResponse,
)
from app.services.sell_conditions_service import (
    get_active_sell_conditions,
    get_sell_condition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sell_condition(
    symbol: str = "000660",
    name: str = "SK하이닉스",
    is_active: bool = True,
    price_threshold: float = 1_152_000.0,
    stoch_rsi_threshold: float = 80.0,
    foreign_days: int = 2,
    rsi_high: float = 70.0,
    rsi_low: float = 65.0,
    bb_upper_ref: float = 1_142_000.0,
) -> MagicMock:
    cond = MagicMock(spec=SellCondition)
    cond.symbol = symbol
    cond.name = name
    cond.is_active = is_active
    cond.price_threshold = price_threshold
    cond.stoch_rsi_threshold = stoch_rsi_threshold
    cond.foreign_days = foreign_days
    cond.rsi_high = rsi_high
    cond.rsi_low = rsi_low
    cond.bb_upper_ref = bb_upper_ref
    return cond


def _make_eval_result(
    symbol: str = "000660",
    name: str = "SK하이닉스",
    triggered: bool = False,
    conditions_met: int = 0,
) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "triggered": triggered,
        "conditions_met": conditions_met,
        "conditions": [],
        "message": f"[매도 {'검토' if triggered else '대기'}] {name} {conditions_met}/5 조건 충족",
        "errors": [],
    }


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)

    async def override_get_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


# ---------------------------------------------------------------------------
# sell_conditions_service — DB CRUD
# ---------------------------------------------------------------------------


class TestSellConditionsService:
    @pytest.mark.asyncio
    async def test_get_sell_condition_returns_match(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        expected = _make_sell_condition()
        mock_result.scalar_one_or_none.return_value = expected
        mock_db.execute.return_value = mock_result

        result = await get_sell_condition(mock_db, "000660")
        assert result is expected
        mock_db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_sell_condition_returns_none_for_missing(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await get_sell_condition(mock_db, "999999")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_sell_conditions_returns_active_only(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        active_conds = [
            _make_sell_condition("000660", "SK하이닉스"),
            _make_sell_condition("005930", "삼성전자"),
        ]
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = active_conds
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await get_active_sell_conditions(mock_db)
        assert len(result) == 2
        assert result[0].symbol == "000660"
        assert result[1].symbol == "005930"

    @pytest.mark.asyncio
    async def test_get_active_sell_conditions_returns_empty(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await get_active_sell_conditions(mock_db)
        assert result == []


# ---------------------------------------------------------------------------
# Batch endpoint — GET /api/n8n/sell-signal/batch
# ---------------------------------------------------------------------------


class TestBatchEndpoint:
    @pytest.mark.asyncio
    async def test_batch_success_with_results(self, client):
        conditions = [
            _make_sell_condition("000660", "SK하이닉스"),
            _make_sell_condition("005930", "삼성전자"),
        ]
        eval_results = [
            _make_eval_result("000660", "SK하이닉스", triggered=True, conditions_met=3),
            _make_eval_result("005930", "삼성전자", triggered=False, conditions_met=1),
        ]

        with (
            patch(
                "app.routers.n8n.get_active_sell_conditions",
                return_value=conditions,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                side_effect=eval_results,
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/batch")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["total"] == 2
            assert data["triggered_count"] == 1
            assert len(data["results"]) == 2

            validated = N8nSellSignalBatchResponse(**data)
            assert validated.triggered_count == 1

    @pytest.mark.asyncio
    async def test_batch_empty_monitoring_list(self, client):
        with patch(
            "app.routers.n8n.get_active_sell_conditions",
            return_value=[],
        ):
            resp = client.get("/api/n8n/sell-signal/batch")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["total"] == 0
            assert data["triggered_count"] == 0
            assert data["results"] == []

    @pytest.mark.asyncio
    async def test_batch_individual_symbol_failure_others_succeed(self, client):
        conditions = [
            _make_sell_condition("000660", "SK하이닉스"),
            _make_sell_condition("005930", "삼성전자"),
            _make_sell_condition("035420", "NAVER"),
        ]

        def side_effect_fn(**kwargs):
            sym = kwargs.get("symbol")
            if sym == "005930":
                raise RuntimeError("삼성전자 API 장애")
            return _make_eval_result(sym, sym, triggered=False, conditions_met=0)

        with (
            patch(
                "app.routers.n8n.get_active_sell_conditions",
                return_value=conditions,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                side_effect=side_effect_fn,
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/batch")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["total"] == 2
            assert len(data["errors"]) == 1
            assert data["errors"][0]["symbol"] == "005930"

    @pytest.mark.asyncio
    async def test_batch_db_failure_returns_500(self, client):
        with patch(
            "app.routers.n8n.get_active_sell_conditions",
            side_effect=RuntimeError("DB connection lost"),
        ):
            resp = client.get("/api/n8n/sell-signal/batch")
            assert resp.status_code == 500
            data = resp.json()
            assert data["success"] is False
            assert data["total"] == 0
            assert len(data["errors"]) > 0

    @pytest.mark.asyncio
    async def test_batch_all_symbols_fail_gracefully(self, client):
        conditions = [_make_sell_condition("000660", "SK하이닉스")]

        with (
            patch(
                "app.routers.n8n.get_active_sell_conditions",
                return_value=conditions,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                side_effect=RuntimeError("total failure"),
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/batch")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["total"] == 0
            assert len(data["errors"]) == 1

    @pytest.mark.asyncio
    async def test_batch_response_validates_as_model(self, client):
        conditions = [_make_sell_condition("000660", "SK하이닉스")]
        eval_result = _make_eval_result(
            "000660", "SK하이닉스", triggered=True, conditions_met=2
        )

        with (
            patch(
                "app.routers.n8n.get_active_sell_conditions",
                return_value=conditions,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=eval_result,
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/batch")
            validated = N8nSellSignalBatchResponse(**resp.json())
            assert validated.total == 1
            assert validated.results[0].symbol == "000660"


# ---------------------------------------------------------------------------
# Single-symbol endpoint — DB default loading
# ---------------------------------------------------------------------------


class TestSingleSymbolDbDefaults:
    @pytest.mark.asyncio
    async def test_loads_defaults_from_db_when_no_query_params(self, client):
        db_cond = _make_sell_condition(
            "000660",
            price_threshold=1_200_000.0,
            stoch_rsi_threshold=75.0,
            foreign_days=3,
            rsi_high=72.0,
            rsi_low=62.0,
            bb_upper_ref=1_180_000.0,
        )
        mock_result = _make_eval_result("000660", "SK하이닉스")

        with (
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=db_cond,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=mock_result,
            ) as mock_eval,
        ):
            resp = client.get("/api/n8n/sell-signal/000660")
            assert resp.status_code == 200
            call_kwargs = mock_eval.call_args[1]
            assert call_kwargs["price_threshold"] == 1_200_000.0
            assert call_kwargs["stoch_rsi_threshold"] == 75.0
            assert call_kwargs["foreign_consecutive_days"] == 3
            assert call_kwargs["rsi_high_mark"] == 72.0
            assert call_kwargs["rsi_low_mark"] == 62.0
            assert call_kwargs["bb_upper_ref"] == 1_180_000.0

    @pytest.mark.asyncio
    async def test_query_params_override_db_values(self, client):
        db_cond = _make_sell_condition(
            "000660",
            price_threshold=1_200_000.0,
            stoch_rsi_threshold=75.0,
        )
        mock_result = _make_eval_result("000660", "SK하이닉스")

        with (
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=db_cond,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=mock_result,
            ) as mock_eval,
        ):
            resp = client.get(
                "/api/n8n/sell-signal/000660",
                params={"price_threshold": 999_000, "stoch_rsi_threshold": 60},
            )
            assert resp.status_code == 200
            call_kwargs = mock_eval.call_args[1]
            assert call_kwargs["price_threshold"] == 999_000
            assert call_kwargs["stoch_rsi_threshold"] == 60

    @pytest.mark.asyncio
    async def test_falls_back_to_hardcoded_defaults_without_db_record(self, client):
        mock_result = _make_eval_result("000660", "SK하이닉스")

        with (
            patch(
                "app.routers.n8n.get_sell_condition",
                return_value=None,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                return_value=mock_result,
            ) as mock_eval,
        ):
            resp = client.get("/api/n8n/sell-signal/000660")
            assert resp.status_code == 200
            call_kwargs = mock_eval.call_args[1]
            assert call_kwargs["price_threshold"] == 1_152_000
            assert call_kwargs["stoch_rsi_threshold"] == 80
            assert call_kwargs["foreign_consecutive_days"] == 2
            assert call_kwargs["rsi_high_mark"] == 70
            assert call_kwargs["rsi_low_mark"] == 65
            assert call_kwargs["bb_upper_ref"] == 1_142_000


# ---------------------------------------------------------------------------
# n8n workflow JSON validation
# ---------------------------------------------------------------------------


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / "n8n" / "workflows" / "sell-alert.json"
)


class TestN8nWorkflowValidation:
    @pytest.fixture
    def workflow(self) -> dict:
        with open(WORKFLOW_PATH) as f:
            return json.load(f)

    def test_workflow_json_is_valid(self):
        assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
        with open(WORKFLOW_PATH) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_required_nodes_exist(self, workflow):
        node_types = {n["type"] for n in workflow["nodes"]}
        assert "n8n-nodes-base.scheduleTrigger" in node_types
        assert "n8n-nodes-base.httpRequest" in node_types
        assert "n8n-nodes-base.if" in node_types

    def test_required_node_names_present(self, workflow):
        node_names = {n["name"] for n in workflow["nodes"]}
        expected = {
            "Schedule Trigger",
            "Fetch Batch Sell Signals",
            "Filter Triggered",
            "Has Triggered?",
            "Format Message",
            "Send to Hermes",
        }
        assert expected.issubset(node_names)

    def test_no_hardcoded_tokens(self, workflow):
        raw = json.dumps(workflow)
        assert "Bearer " not in raw
        assert "eyJ" not in raw  # JWT pattern

    def test_uses_env_variables_for_urls(self, workflow):
        raw = json.dumps(workflow)
        assert "$env.AUTO_TRADER_API_URL" in raw
        assert "$env.HERMES_WEBHOOK_URL" in raw
        assert "http://localhost" not in raw
        assert "https://" not in raw.replace("https://", "", 0)

    def test_credential_store_reference(self, workflow):
        http_nodes = [n for n in workflow["nodes"] if "httpRequest" in n["type"]]
        fetch_node = next(
            n for n in http_nodes if n["name"] == "Fetch Batch Sell Signals"
        )
        assert "credentials" in fetch_node
        creds = fetch_node["credentials"]
        assert "httpHeaderAuth" in creds
        assert creds["httpHeaderAuth"]["id"] == "auto_trader_api_token"

    def test_connections_form_valid_pipeline(self, workflow):
        connections = workflow["connections"]
        assert "Schedule Trigger" in connections
        assert "Fetch Batch Sell Signals" in connections
        assert "Filter Triggered" in connections
        assert "Has Triggered?" in connections
        assert "Format Message" in connections

        first_hop = connections["Schedule Trigger"]["main"][0][0]["node"]
        assert first_hop == "Fetch Batch Sell Signals"

    def test_schedule_interval_is_5_minutes(self, workflow):
        trigger = next(n for n in workflow["nodes"] if n["name"] == "Schedule Trigger")
        intervals = trigger["parameters"]["rule"]["interval"]
        assert intervals[0]["minutesInterval"] == 5

    def test_hermes_node_sends_text_field(self, workflow):
        hermes = next(n for n in workflow["nodes"] if n["name"] == "Send to Hermes")
        params = hermes["parameters"]
        assert params["method"] == "POST"
        assert params["sendBody"] is True
        body_params = params["bodyParameters"]["parameters"]
        text_param = next(p for p in body_params if p["name"] == "text")
        assert "$json.text" in text_param["value"]
