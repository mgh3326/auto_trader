from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestN8nFollowupEndpoints:
    def _get_client(self) -> TestClient:
        app = FastAPI()
        from app.routers.n8n import router

        app.include_router(router)
        return TestClient(app)

    def test_tc_followup_returns_preliminary_render(self) -> None:
        client = self._get_client()

        resp = client.post(
            "/api/n8n/tc-followup",
            json={
                "manual_cash_krw": 1_250_000,
                "daily_burn_krw": 50_000,
                "weights_top_n": [{"symbol": "BTC", "weight_pct": 42.5}],
                "holdings": [
                    {"symbol": "BTC", "current_krw_value": 1_000_000, "dust": False}
                ],
                "dust_items": [
                    {"symbol": "DOGE", "current_krw_value": 3_000, "dust": True}
                ],
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "tc_preliminary"
        assert body["generated_at"]
        assert body["embed"]["title"] == "📊 TC Preliminary — 자금 현황 재계산"
        assert "경로 A·B 병행 가능" in body["text"]
        assert "BTC" in body["text"]
        assert "DOGE" in body["text"]
        assert "🎯 권고" not in body["text"]
        assert "📊 Gate 판정 결과" not in body["text"]

    def test_cio_followup_returns_pending_render_with_gates(self) -> None:
        client = self._get_client()

        resp = client.post(
            "/api/n8n/cio-followup",
            json={
                "manual_cash_krw": 700_000,
                "daily_burn_krw": 100_000,
                "manual_cash_runway_days": 7,
                "funding_intent": "runway_recovery",
                "board_response": {
                    "amount": 1_000_000,
                    "target": "cash",
                    "funding_intent": "runway_recovery",
                    "manual_cash_verified": True,
                },
                "g1_gate": {
                    "force_cash_policy_note": "(3) 현금 우선 정책 적용",
                    "symbols": {
                        "BTC": {
                            "data_sufficient": False,
                            "missing": ["14d_ohlcv"],
                        }
                    },
                },
                "weights_top_n": [{"symbol": "BTC", "weight_pct": 42.5}],
                "holdings": [
                    {"symbol": "BTC", "current_krw_value": 1_000_000, "dust": False}
                ],
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "cio_pending"
        assert body["embed"]["title"] == "🎯 CIO Pending Decision — Gate 판정 결과"
        assert body["gate_results"]["G1"]["status"] == "fail"
        assert body["gate_results"]["G2"]["passed"] is False
        assert "🚫 신규 매수 차단 — G2 fail" in body["text"]
        assert "(3) 현금 우선 정책 적용" in body["text"]
        assert "📊 Gate 판정 결과" in body["text"]
        assert "[funding]" in body["text"]
        assert "[action]" in body["text"]

    @pytest.mark.parametrize(
        ("payload", "field"),
        [
            ({"board_response": {"amount": -1, "funding_intent": "new_buy"}}, "amount"),
            (
                {
                    "board_response": {
                        "amount": 100_000_000_001,
                        "funding_intent": "new_buy",
                    }
                },
                "amount",
            ),
            ({"funding_intent": "invalid"}, "funding_intent"),
        ],
    )
    def test_cio_followup_validates_input(self, payload: dict, field: str) -> None:
        client = self._get_client()

        resp = client.post("/api/n8n/cio-followup", json=payload)

        assert resp.status_code == 422
        assert field in resp.text
