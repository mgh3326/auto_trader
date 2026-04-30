"""Unit tests for preopen_dashboard_service (ROB-39)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest


def _make_candidate(**kwargs) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "candidate_uuid": uuid4(),
        "symbol": "005930",
        "instrument_type": SimpleNamespace(value="equity_kr"),
        "side": "buy",
        "candidate_kind": "proposed",
        "proposed_price": Decimal("70000"),
        "proposed_qty": Decimal("10"),
        "confidence": 75,
        "rationale": "Strong momentum",
        "currency": "KRW",
        "warnings": [],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_reconciliation(**kwargs) -> SimpleNamespace:
    defaults = {
        "order_id": "ORD-1",
        "symbol": "005930",
        "market": "kr",
        "side": "buy",
        "classification": "near_fill",
        "nxt_classification": "buy_pending_actionable",
        "nxt_actionable": True,
        "gap_pct": Decimal("0.50"),
        "reasons": ["gap_within_near_fill_pct"],
        "warnings": [],
        "summary": "Gap within near fill threshold",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_run(**kwargs) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "run_uuid": uuid4(),
        "user_id": 7,
        "market_scope": "kr",
        "stage": "preopen",
        "status": "open",
        "source_profile": "roadmap",
        "strategy_name": "Morning scan",
        "notes": None,
        "market_brief": {"summary": "Cautious"},
        "source_freshness": None,
        "source_warnings": [],
        "advisory_links": [{"provider": "research"}],
        "generated_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
        "candidates": [_make_candidate()],
        "reconciliations": [_make_reconciliation()],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_news_readiness(**kwargs) -> SimpleNamespace:
    defaults = {
        "market": "kr",
        "is_ready": True,
        "is_stale": False,
        "latest_run_uuid": "news-run",
        "latest_status": "success",
        "latest_finished_at": datetime.now(UTC),
        "latest_article_published_at": datetime.now(UTC),
        "source_counts": {"browser_naver_mainnews": 20},
        "warnings": [],
        "max_age_minutes": 180,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_returns_fail_open_when_no_run():
    from app.services import preopen_dashboard_service, research_run_service

    with patch.object(
        research_run_service,
        "get_latest_research_run",
        new=AsyncMock(return_value=None),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.has_run is False
    assert result.advisory_used is False
    assert result.advisory_skipped_reason == "no_open_preopen_run"
    assert result.candidates == []
    assert result.reconciliations == []
    assert result.linked_sessions == []
    assert result.run_uuid is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maps_candidates_and_reconciliations():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run(
        candidates=[
            _make_candidate(symbol="005930", side="buy", confidence=80),
            _make_candidate(
                id=2,
                candidate_uuid=uuid4(),
                symbol="000660",
                side="sell",
                confidence=60,
            ),
        ],
        reconciliations=[
            _make_reconciliation(symbol="005930", classification="near_fill"),
        ],
    )

    with (
        patch.object(
            research_run_service,
            "get_latest_research_run",
            new=AsyncMock(return_value=run),
        ),
        patch.object(
            preopen_dashboard_service,
            "_linked_sessions",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_news_readiness",
            new=AsyncMock(return_value=_make_news_readiness()),
        ),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.has_run is True
    assert result.advisory_used is True
    assert result.candidate_count == 2
    assert result.reconciliation_count == 1
    # buy comes before sell in ordering
    assert result.candidates[0].side == "buy"
    assert result.candidates[1].side == "sell"
    assert result.reconciliations[0].symbol == "005930"
    assert result.reconciliations[0].gap_pct == Decimal("0.50")
    assert result.run_uuid == run.run_uuid


@pytest.mark.asyncio
@pytest.mark.unit
async def test_advisory_skipped_reason_when_zero_candidates():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run(candidates=[])

    with (
        patch.object(
            research_run_service,
            "get_latest_research_run",
            new=AsyncMock(return_value=run),
        ),
        patch.object(
            preopen_dashboard_service,
            "_linked_sessions",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_news_readiness",
            new=AsyncMock(return_value=_make_news_readiness()),
        ),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.has_run is True
    assert result.advisory_used is False
    assert result.advisory_skipped_reason == "no_candidates"
    assert result.candidate_count == 0


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "warning",
    ["advisory_timeout", "tradingagents_not_configured"],
)
async def test_advisory_skipped_reason_from_source_warning(warning: str):
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run(source_warnings=[warning])

    with (
        patch.object(
            research_run_service,
            "get_latest_research_run",
            new=AsyncMock(return_value=run),
        ),
        patch.object(
            preopen_dashboard_service,
            "_linked_sessions",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_news_readiness",
            new=AsyncMock(return_value=_make_news_readiness()),
        ),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.has_run is True
    assert result.advisory_used is False
    assert result.advisory_skipped_reason == warning


@pytest.mark.asyncio
@pytest.mark.unit
async def test_linked_sessions_lookup_fail_open():
    """linked_sessions returns [] if query fails."""
    from app.services import preopen_dashboard_service

    db_mock = AsyncMock()
    db_mock.execute.side_effect = RuntimeError("DB unavailable")

    run = _make_run()
    result = await preopen_dashboard_service._linked_sessions(
        db_mock, run=run, user_id=7
    )
    assert result == []


@pytest.mark.unit
def test_no_forbidden_imports():
    """preopen modules must not import broker/order/watch/intent/credential modules."""
    import app.routers.preopen as router_mod
    import app.services.preopen_dashboard_service as svc_mod

    forbidden_parts = (
        "kis",
        "upbit",
        "broker",
        "order_service",
        "order_tool",
        "trading_service",
        "watch",
        "alert",
        "intent",
        "credential",
        "token_manager",
    )

    for mod in (router_mod, svc_mod):
        tree = ast.parse(Path(mod.__file__).read_text())
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]

            for name in imported:
                low = name.lower()
                assert not any(part in low for part in forbidden_parts), (
                    f"Forbidden import '{name}' found in {mod.__name__}"
                )
