from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from app.schemas.n8n.board_brief import BoardBriefContext, GateResult, N8nG2GatePayload
from app.services.n8n_daily_brief_service import RenderRouter


def plan_v2_section_f_dataset(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "exchange_krw": 83_318,
        "unverified_cap": {
            "amount": 10_000_000,
            "confirmed_at": None,
            "verified_by_boss_today": False,
            "stale_warning": True,
        },
        "manual_cash_krw": 10_000_000,
        "daily_burn_krw": 80_000,
        "next_obligation": {
            "date": "2026-04-29",
            "days_remaining": 12,
            "cash_needed_until": 960_000,
        },
        "tier_scenarios": [
            {
                "label": "T1",
                "deposit_amount": 516_682,
                "target_exchange_krw": 600_000,
                "buffer_days": 7,
                "cushion_after_obligation": -360_000,
            },
            {
                "label": "T2",
                "deposit_amount": 1_116_682,
                "target_exchange_krw": 1_200_000,
                "buffer_days": 15,
                "cushion_after_obligation": 240_000,
            },
            {
                "label": "T3",
                "deposit_amount": 2_316_682,
                "target_exchange_krw": 2_400_000,
                "buffer_days": 30,
                "cushion_after_obligation": 1_440_000,
            },
        ],
        "hard_gate_candidates": [
            {
                "symbol": "SOL",
                "proposal": "SOL 현물 8~10개 부분매도",
                "amount_range": "1.13~1.40M KRW",
            }
        ],
        "data_sufficient_by_symbol": {"BTC": True, "SOL": True},
        "btc_regime": {
            "close_vs_20d_ma": "above",
            "ma20_slope": "flat",
            "drawdown_14d_pct": -3.2,
        },
        "weights_top_n": [
            {"symbol": "SOL", "weight_pct": 32},
            {"symbol": "ETH", "weight_pct": 28},
            {"symbol": "BTC", "weight_pct": 11},
            {"symbol": "XRP", "weight_pct": 11},
            {"symbol": "LINK", "weight_pct": 10},
        ],
        "holdings": [
            {"symbol": "SOL", "current_krw_value": 3_200_000, "dust": False},
            {"symbol": "ETH", "current_krw_value": 2_800_000, "dust": False},
            {"symbol": "BTC", "current_krw_value": 1_100_000, "dust": False},
            {"symbol": "XRP", "current_krw_value": 1_100_000, "dust": False},
            {"symbol": "LINK", "current_krw_value": 1_000_000, "dust": False},
            {"symbol": "APT", "current_krw_value": 654, "dust": True},
        ],
        "dust_items": [{"symbol": "APT", "current_krw_value": 654}],
        "gate_results": {
            "G1": GateResult(status="pass", detail="데이터 충분"),
            "G2": N8nG2GatePayload(
                passed=True,
                status="pass",
                detail="운영 runway 복구",
            ),
            "G3": GateResult(status="pass", detail="cushion 충족"),
            "G4": GateResult(status="pass", detail="BTC regime 통과"),
            "G5": GateResult(status="pass", detail="volatility halt 없음"),
            "G6": GateResult(status="pass", detail="RSI=45 보조지표 통과"),
        },
    }
    payload.update(updates)
    return payload


def plan_v2_section_f_context(**updates: Any) -> BoardBriefContext:
    return BoardBriefContext.model_validate(plan_v2_section_f_dataset(**updates))


def drop_required_field(field: str) -> dict[str, Any]:
    payload = deepcopy(plan_v2_section_f_dataset())
    if field in {
        "exchange_krw",
        "unverified_cap",
        "next_obligation",
        "tier_scenarios",
        "data_sufficient_by_symbol",
        "btc_regime",
        "holdings",
    }:
        payload.pop(field)
    else:
        raise ValueError(f"Unknown required field: {field}")
    return payload


def replace_once(old: str, new: str) -> Callable[[str], str]:
    def _postprocess(text: str) -> str:
        assert old in text
        return text.replace(old, new, 1)

    return _postprocess


class RecordingRouter(RenderRouter):
    def __init__(self) -> None:
        self.board_messages: list[str] = []
        self.ops_messages: list[str] = []

    def route_board(self, message: str) -> None:
        self.board_messages.append(message)

    def route_ops_escalation(self, message: str) -> None:
        self.ops_messages.append(message)
