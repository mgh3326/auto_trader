"""Unit tests for preopen_dashboard_service (ROB-39)."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
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


@contextmanager
def _patched_dashboard_dependencies(
    preopen_dashboard_service,
    research_run_service,
    *,
    run,
    readiness=None,
    preview=None,
    readiness_error: Exception | None = None,
    preview_error: Exception | None = None,
):
    readiness_mock = (
        AsyncMock(side_effect=readiness_error)
        if readiness_error
        else AsyncMock(
            return_value=readiness if readiness is not None else _make_news_readiness()
        )
    )
    preview_mock = (
        AsyncMock(side_effect=preview_error)
        if preview_error
        else AsyncMock(return_value=preview if preview is not None else [])
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
            new=readiness_mock,
        ),
        patch.object(
            preopen_dashboard_service,
            "get_latest_news_preview",
            new=preview_mock,
        ),
    ):
        yield


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

    with _patched_dashboard_dependencies(
        preopen_dashboard_service, research_run_service, run=run
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

    with _patched_dashboard_dependencies(
        preopen_dashboard_service, research_run_service, run=run
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

    with _patched_dashboard_dependencies(
        preopen_dashboard_service, research_run_service, run=run
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
async def test_linked_sessions_lookup_maps_recent_sessions():
    """linked_sessions maps DB rows into LinkedSessionRef values."""
    from app.services import preopen_dashboard_service

    created_at = datetime.now(UTC)
    session = SimpleNamespace(
        session_uuid=uuid4(),
        status="open",
        created_at=created_at,
    )
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [session]
    db_mock = AsyncMock()
    db_mock.execute.return_value = result_mock

    run = _make_run()
    result = await preopen_dashboard_service._linked_sessions(
        db_mock, run=run, user_id=7
    )

    assert len(result) == 1
    assert result[0].session_uuid == session.session_uuid
    assert result[0].status == "open"
    assert result[0].created_at == created_at


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
def test_derive_news_status_falls_back_to_stale_for_ambiguous_not_ready():
    """Ambiguous non-ready readiness payloads must degrade to stale, not ready."""
    from app.services import preopen_dashboard_service

    readiness = _make_news_readiness(
        is_ready=False,
        is_stale=False,
        warnings=[],
        latest_run_uuid="run-ambiguous",
    )

    assert preopen_dashboard_service._derive_news_status(readiness) == "stale"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_ready_and_preview_attached():
    from app.schemas.preopen import NewsArticlePreview
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(
        is_ready=True,
        is_stale=False,
        warnings=[],
        source_counts={"browser_naver_mainnews": 20, "yna_market": 12},
    )
    preview = [
        NewsArticlePreview(
            id=1,
            title="t",
            url="u",
            source="MK",
            feed_source="mk_stock",
            published_at=datetime.now(UTC),
            summary=None,
        )
    ]

    with _patched_dashboard_dependencies(
        preopen_dashboard_service,
        research_run_service,
        run=run,
        readiness=readiness,
        preview=preview,
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "ready"
    assert result.news.source_counts["browser_naver_mainnews"] == 20
    assert len(result.news_preview) == 1
    assert result.news_preview[0].title == "t"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_stale_status_when_warning_present():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(
        is_ready=False,
        is_stale=True,
        warnings=["news_stale"],
        source_counts={"browser_naver_mainnews": 20},
    )

    with _patched_dashboard_dependencies(
        preopen_dashboard_service,
        research_run_service,
        run=run,
        readiness=readiness,
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "stale"
    assert "news_stale" in result.news.warnings


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_unavailable_when_no_run():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(
        is_ready=False,
        is_stale=True,
        latest_run_uuid=None,
        latest_status=None,
        latest_finished_at=None,
        warnings=["news_unavailable", "news_stale"],
        source_counts={},
    )

    with _patched_dashboard_dependencies(
        preopen_dashboard_service,
        research_run_service,
        run=run,
        readiness=readiness,
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "unavailable"
    assert result.news_preview == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_none_when_readiness_lookup_raises():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()

    with _patched_dashboard_dependencies(
        preopen_dashboard_service,
        research_run_service,
        run=run,
        readiness_error=RuntimeError("redis down"),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.news is None
    assert result.news_preview == []
    assert "news_readiness_unavailable" in result.source_warnings


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_preview_lookup_failure_keeps_readiness_summary():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(source_counts={"browser_naver_mainnews": 20})

    with _patched_dashboard_dependencies(
        preopen_dashboard_service,
        research_run_service,
        run=run,
        readiness=readiness,
        preview_error=RuntimeError("preview query failed"),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "ready"
    assert result.news_preview == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_unavailable_does_not_demote_other_freshness_signals():
    """news_unavailable must only modify the 'news' slot of source_freshness.

    Other source freshness entries (e.g. kis, upbit) must be preserved
    unchanged, and only news_* warnings should be appended to source_warnings.
    """
    from app.services import preopen_dashboard_service, research_run_service

    pre_existing_freshness = {
        "kis": {"ok": True, "latency_ms": 12},
        "upbit": {"ok": True, "latency_ms": 8},
    }
    run = _make_run(
        source_freshness=pre_existing_freshness,
        source_warnings=[],
    )
    readiness = _make_news_readiness(
        is_ready=False,
        is_stale=True,
        latest_run_uuid=None,
        latest_status=None,
        latest_finished_at=None,
        warnings=["news_unavailable", "news_stale"],
        source_counts={},
    )

    with _patched_dashboard_dependencies(
        preopen_dashboard_service,
        research_run_service,
        run=run,
        readiness=readiness,
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.source_freshness is not None
    # Non-news entries must be preserved verbatim
    assert result.source_freshness["kis"] == {"ok": True, "latency_ms": 12}
    assert result.source_freshness["upbit"] == {"ok": True, "latency_ms": 8}
    # Only the 'news' slot should be added
    assert "news" in result.source_freshness
    assert result.source_freshness["news"]["is_ready"] is False
    # No unrelated warnings were injected
    non_news = [w for w in result.source_warnings if not w.startswith("news_")]
    assert non_news == [], f"Unexpected non-news warnings injected: {non_news}"
    assert "news_unavailable" in result.source_warnings


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
