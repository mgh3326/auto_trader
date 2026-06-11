"""ROB-265 Plan 3 — MCP handler tests.

Each tool's ``*_impl`` is called directly (matches the legacy
``test_analysis_report_workflow.py`` style). The handlers open their
own ``AsyncSessionLocal`` against the same test_db that the ``session``
fixture manages; the fixture's per-test TRUNCATE keeps state clean.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.investment_reports_handlers import (
    INVESTMENT_REPORT_TOOL_NAMES,
    investment_report_activate_watch_impl,
    investment_report_add_items_impl,
    investment_report_context_get_impl,
    investment_report_create_impl,
    investment_report_decide_item_impl,
    investment_report_delta_get_impl,
    investment_report_generate_from_bundle_impl,
    investment_report_get_impl,
    investment_report_list_impl,
    investment_report_set_status_impl,
    investment_report_update_impl,
    investment_watch_recommend_impl,
)
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from tests._investment_reports_helpers import future_datetime


async def _publish_by_uuid(report_uuid: str) -> None:
    """ROB-352: set status='published', clearing snapshot_freshness_summary to SQL
    NULL so the DB CHECK constraint is satisfied. Opens and commits its own session
    so the change is visible to subsequent MCP handler sessions.
    Direct SQL avoids asyncpg serialising Python None → JSON null for JSONB columns.
    """
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(
            sa.text(
                "UPDATE review.investment_reports"
                " SET status = 'published', snapshot_freshness_summary = NULL"
                " WHERE report_uuid = :uuid"
            ).bindparams(uuid=uuid.UUID(report_uuid))
        )
        await db.commit()


def _create_kwargs(
    *, kst_date: str = "2026-05-18", market: str = "kr", **overrides
) -> dict:
    kwargs: dict = {
        "report_type": "kr_morning",
        "market": market,
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": f"t-{kst_date}",
        "summary": "s",
        "kst_date": kst_date,
    }
    kwargs.update(overrides)
    return kwargs


def _action_item_dict(client_item_key: str = "action-1") -> dict:
    return {
        "client_item_key": client_item_key,
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "rationale": "r",
    }


def _watch_item_dict(client_item_key: str = "watch-1") -> dict:
    return {
        "client_item_key": client_item_key,
        "item_kind": "watch",
        "symbol": "000660",
        "intent": "trend_recovery_review",
        "rationale": "r",
        "watch_condition": {
            "metric": "rsi",
            "operator": "below",
            "threshold": 30,
        },
        "valid_until": future_datetime().isoformat(),
    }


def _review_watch_item_dict(client_item_key: str = "review-watch-1") -> dict:
    """operation='review' watch — 생성 시 watch_condition/valid_until 면제(ROB-274).

    ROB-393 재현용: 이 항목은 condition 없이 approve까지 도달하지만 종전
    activate_watch에서 'corrupt state'로 막혔다.
    """
    return {
        "client_item_key": client_item_key,
        "item_kind": "watch",
        "operation": "review",
        "symbol": "005930",
        "intent": "trend_recovery_review",
        "rationale": "r",
    }


def test_tool_names_match_registered_set() -> None:
    assert INVESTMENT_REPORT_TOOL_NAMES == {
        "investment_report_create",
        "investment_report_list",
        "investment_report_get",
        "investment_report_decide_item",
        "investment_report_activate_watch",
        "investment_report_context_get",
        # ROB-273 — opt-in snapshot-backed advisory generator.
        "investment_report_generate_from_bundle",
        "investment_watch_recommend",
        "investment_report_delta_get",
        # ROB-455 — report status lifecycle transition writer.
        "investment_report_set_status",
        "investment_report_add_items",
        "investment_report_update",
    }


@pytest.mark.asyncio
async def test_create_returns_idempotent_false_on_first_call(
    session: AsyncSession,
) -> None:
    response = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs()
    )
    assert response["success"] is True
    assert response["idempotent"] is False
    assert response["report"]["report_type"] == "kr_morning"
    assert response["report"]["market"] == "kr"


@pytest.mark.asyncio
async def test_create_returns_idempotent_true_on_replay(
    session: AsyncSession,
) -> None:
    first = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs()
    )
    second = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs()
    )
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["report"]["report_uuid"] == second["report"]["report_uuid"]


@pytest.mark.asyncio
async def test_list_filters_propagate(session: AsyncSession) -> None:
    await investment_report_create_impl(
        items=[_action_item_dict()],
        **_create_kwargs(market="kr", kst_date="2026-05-18"),
    )
    await investment_report_create_impl(
        items=[_action_item_dict()],
        **_create_kwargs(market="us", kst_date="2026-05-18"),
    )
    response = await investment_report_list_impl(market="kr")
    assert response["success"] is True
    assert len(response["reports"]) == 1
    assert response["reports"][0]["market"] == "kr"


@pytest.mark.asyncio
async def test_list_returns_summary_only_and_paginates(session: AsyncSession) -> None:
    # ROB-465: list returns lightweight summaries (no heavy report bodies) and
    # paginates via limit/offset so the response stays inside the token budget.
    for d in ("2026-05-18", "2026-05-19", "2026-05-20"):
        await investment_report_create_impl(
            items=[_action_item_dict()],
            **_create_kwargs(market="kr", kst_date=d),
        )

    page1 = await investment_report_list_impl(market="kr", limit=2)
    assert page1["success"] is True
    assert len(page1["reports"]) == 2

    summary = page1["reports"][0]
    # summary-only: heavy bodies are dropped, key identifiers retained.
    assert {"report_uuid", "title", "status", "kst_date"} <= set(summary.keys())
    assert "market_snapshot" not in summary
    assert "portfolio_snapshot" not in summary
    assert "report_metadata" not in summary

    pg = page1["pagination"]
    assert pg["returned_count"] == 2
    assert pg["offset"] == 0
    assert pg["limit"] == 2
    assert pg["has_more"] is True
    assert pg["next_offset"] == 2

    page2 = await investment_report_list_impl(market="kr", limit=2, offset=2)
    assert len(page2["reports"]) == 1
    assert page2["pagination"]["has_more"] is False
    assert page2["pagination"]["next_offset"] is None


@pytest.mark.asyncio
async def test_get_returns_not_found_for_unknown(session: AsyncSession) -> None:
    response = await investment_report_get_impl(str(uuid.uuid4()))
    assert response == {"success": False, "error": "not_found"}


@pytest.mark.asyncio
async def test_get_returns_bundle_with_nested_decisions(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("act-1"), _watch_item_dict("wt-1")],
        **_create_kwargs(),
    )
    report_uuid = created["report"]["report_uuid"]

    # Look up the items by reading the bundle once.
    bundle_pre = await investment_report_get_impl(report_uuid)
    assert bundle_pre["success"] is True
    item_uuids = {it["item_kind"]: it["item_uuid"] for it in bundle_pre["items"]}

    # Approve the action item via MCP.
    await investment_report_decide_item_impl(
        item_uuid=item_uuids["action"], decision="approve", actor="operator-test"
    )

    bundle_post = await investment_report_get_impl(report_uuid)
    decisions = bundle_post["decisions_by_item_uuid"][item_uuids["action"]]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "approve"


@pytest.mark.asyncio
async def test_decide_item_idempotent_per_default_key(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    first = await investment_report_decide_item_impl(
        item_uuid=item_uuid, decision="approve", actor="operator"
    )
    second = await investment_report_decide_item_impl(
        item_uuid=item_uuid, decision="approve", actor="operator"
    )
    assert first["decision"]["decision_uuid"] == second["decision"]["decision_uuid"]


@pytest.mark.asyncio
async def test_decide_item_partial_approve_requires_payload(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    with pytest.raises(Exception) as exc_info:
        await investment_report_decide_item_impl(
            item_uuid=item_uuid, decision="partial_approve", actor="operator"
        )
    assert "approved_payload_snapshot" in str(exc_info.value)


@pytest.mark.asyncio
async def test_activate_watch_copies_snapshot(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid, actor="operator"
    )
    assert response["success"] is True
    assert response["alert"]["source_item_uuid"] == watch_uuid
    assert response["alert"]["metric"] == "rsi"
    assert response["alert"]["operator"] == "below"
    assert response["item"]["status"] == "activated"


@pytest.mark.asyncio
async def test_activate_review_watch_without_condition_is_actionable(
    session: AsyncSession,
) -> None:
    """ROB-393 재현: operation='review' watch는 condition 없이 approve되지만,
    인자 없이 activate하면 'corrupt state'가 아니라 actionable 에러여야 한다."""
    created = await investment_report_create_impl(
        items=[_review_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    with pytest.raises(ValueError) as exc_info:
        await investment_report_activate_watch_impl(
            item_uuid=watch_uuid, actor="operator"
        )
    message = str(exc_info.value)
    assert "corrupt state" not in message
    assert "watch_condition not set" in message


@pytest.mark.asyncio
async def test_activate_review_watch_with_injected_condition_succeeds(
    session: AsyncSession,
) -> None:
    """ROB-393: review-watch도 activate 시 watch_condition/valid_until을 주면
    활성화되고, 주입된 조건이 item에 영속화된다."""
    created = await investment_report_create_impl(
        items=[_review_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
    )
    assert response["success"] is True
    assert response["alert"]["metric"] == "price"
    assert response["alert"]["operator"] == "below"
    assert response["item"]["status"] == "activated"

    # 주입 조건이 item에 영속화되었는지 확인.
    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    item_post = bundle_post["items"][0]
    assert item_post["watch_condition"]["metric"] == "price"
    assert item_post["valid_until"] is not None


@pytest.mark.asyncio
async def test_activate_watch_rejects_condition_override(
    session: AsyncSession,
) -> None:
    """ROB-393: condition이 이미 있는 watch에 activate로 또 주면 silent override
    하지 않고 거부한다."""
    created = await investment_report_create_impl(
        items=[_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    with pytest.raises(ValueError) as exc_info:
        await investment_report_activate_watch_impl(
            item_uuid=watch_uuid,
            actor="operator",
            watch_condition={"metric": "price", "operator": "below", "threshold": 1},
        )
    assert "already set" in str(exc_info.value)


@pytest.mark.asyncio
async def test_context_get_aggregates_across_prior_reports(
    session: AsyncSession,
) -> None:
    r1 = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs(kst_date="2026-05-16")
    )
    r2 = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs(kst_date="2026-05-17")
    )
    r3 = await investment_report_create_impl(
        items=[_action_item_dict()], **_create_kwargs(kst_date="2026-05-18")
    )
    # ROB-352: publish r1 and r2 so they appear in prior context (drafts excluded).
    await _publish_by_uuid(r1["report"]["report_uuid"])
    await _publish_by_uuid(r2["report"]["report_uuid"])
    bundle_r1 = await investment_report_get_impl(r1["report"]["report_uuid"])
    await investment_report_decide_item_impl(
        item_uuid=bundle_r1["items"][0]["item_uuid"],
        decision="defer",
        actor="operator",
    )

    ctx = await investment_report_context_get_impl(
        market="kr", exclude_report_uuid=r3["report"]["report_uuid"], n_prior=5
    )
    assert ctx["success"] is True
    prior_uuids = {r["report_uuid"] for r in ctx["prior_reports"]}
    assert prior_uuids == {r1["report"]["report_uuid"], r2["report"]["report_uuid"]}
    deferred_item_uuids = {it["item_uuid"] for it in ctx["unresolved_deferred_items"]}
    assert deferred_item_uuids == {bundle_r1["items"][0]["item_uuid"]}


@pytest.mark.asyncio
async def test_context_get_n_prior_clamped_to_ten(session: AsyncSession) -> None:
    # 15 reports, asking for 50 prior — should be clamped to 10.
    for i in range(15):
        r = await investment_report_create_impl(
            items=[_action_item_dict()],
            **_create_kwargs(kst_date=f"2026-05-{i + 1:02d}"),
        )
        # ROB-352: publish each report so it appears in prior context (drafts excluded).
        await _publish_by_uuid(r["report"]["report_uuid"])
    ctx = await investment_report_context_get_impl(market="kr", n_prior=50)
    assert len(ctx["prior_reports"]) == 10


# ---------------------------------------------------------------------------
# ROB-274 — pending_orders enrichment in context_get
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_context_get_includes_pending_orders_when_collector_succeeds(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """ROB-274 — context response surfaces pending_orders snapshot when fresh."""
    import datetime as dt
    from unittest.mock import AsyncMock

    from app.services.investment_snapshots.collectors import (
        SnapshotCollectorRegistry,
        SnapshotCollectResult,
    )

    fake_orders = [
        {
            "target_ref": {
                "type": "broker_order",
                "broker": "upbit",
                "id": "O1",
                "raw": {},
            },
            "symbol": "KRW-BTC",
            "side": "buy",
            "price": "100",
            "quantity": "0.01",
            "remaining_quantity": "0.01",
            "placed_at": None,
            "stale": False,
            "market": "crypto",
        }
    ]
    fake_result = SnapshotCollectResult(
        snapshot_kind="pending_orders",
        market="crypto",
        account_scope="upbit_live",
        source_kind="auto_trader_mcp",
        payload_json={"pending_orders": fake_orders, "count": 1},
        as_of=dt.datetime.now(tz=dt.UTC),
        freshness_status="fresh",
    )
    fake_collector = AsyncMock()
    fake_collector.collect = AsyncMock(return_value=[fake_result])

    def _fake_registry(_db: object) -> SnapshotCollectorRegistry:
        reg = SnapshotCollectorRegistry()
        reg.register(fake_collector)
        # The registry asks for ``snapshot_kind`` — give the AsyncMock one.
        return reg

    # The collector returned by ``registry.get(...)`` is identified by its
    # ``snapshot_kind`` attribute on register; the AsyncMock needs that set.
    fake_collector.snapshot_kind = "pending_orders"

    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.collectors.registry."
        "production_collector_registry",
        _fake_registry,
    )

    # Need at least one report so the context endpoint has something to
    # serialise — the assertion below only inspects pending_orders.
    await investment_report_create_impl(
        items=[_action_item_dict()],
        **_create_kwargs(market="crypto", kst_date="2026-05-18"),
    )

    ctx = await investment_report_context_get_impl(
        market="crypto", account_scope="upbit_live"
    )
    assert ctx["success"] is True
    assert ctx["pending_orders"] == fake_orders


@pytest.mark.asyncio
async def test_context_get_surfaces_pending_orders_unavailable_as_null(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """ROB-274 — collector reports unavailable → response carries null pending_orders."""
    import datetime as dt
    from unittest.mock import AsyncMock

    from app.services.investment_snapshots.collectors import (
        SnapshotCollectorRegistry,
        SnapshotCollectResult,
    )

    unavailable_result = SnapshotCollectResult(
        snapshot_kind="pending_orders",
        market="crypto",
        account_scope="upbit_live",
        source_kind="auto_trader_mcp",
        payload_json={},
        errors_json={"reason": "upbit_client_unavailable"},
        as_of=dt.datetime.now(tz=dt.UTC),
        freshness_status="unavailable",
    )
    fake_collector = AsyncMock()
    fake_collector.snapshot_kind = "pending_orders"
    fake_collector.collect = AsyncMock(return_value=[unavailable_result])

    def _fake_registry(_db: object) -> SnapshotCollectorRegistry:
        reg = SnapshotCollectorRegistry()
        reg.register(fake_collector)
        return reg

    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.collectors.registry."
        "production_collector_registry",
        _fake_registry,
    )

    await investment_report_create_impl(
        items=[_action_item_dict()],
        **_create_kwargs(market="crypto", kst_date="2026-05-18"),
    )

    ctx = await investment_report_context_get_impl(
        market="crypto", account_scope="upbit_live"
    )
    assert ctx["success"] is True
    assert ctx["pending_orders"] is None


# ---------------------------------------------------------------------------
# ROB-318 — generate_from_bundle must forward user_id so the kis_live
# portfolio collector is invoked instead of staying fail-closed
# ('unavailable'), which otherwise stale-gates the report to advisory_only
# with the generic "포지션 데이터 확인 불가" reason.
# ---------------------------------------------------------------------------
def _capture_generate(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    import uuid as _uuid

    from app.services.action_report.snapshot_backed import generator as gen_mod
    from app.services.action_report.snapshot_backed.request import (
        ReportGenerationRequest,
        ReportGenerationResponse,
    )

    async def _fake_generate(
        _self: object, request: ReportGenerationRequest
    ) -> ReportGenerationResponse:
        captured["request"] = request
        return ReportGenerationResponse(
            report_uuid=_uuid.uuid4(),
            snapshot_bundle_uuid=_uuid.uuid4(),
            snapshot_policy_version=request.policy_version,
            snapshot_coverage_summary={},
            snapshot_freshness_summary={},
            source_conflicts={},
            unavailable_sources={},
            items_count=0,
            warnings=[],
            bundle_status="ok",
            bundle_reused=False,
            stale_gate={},
        )

    monkeypatch.setattr(
        gen_mod.SnapshotBackedReportGenerator, "generate", _fake_generate
    )


@pytest.mark.asyncio
async def test_generate_from_bundle_threads_user_id_to_request(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """ROB-318 — explicit user_id reaches ReportGenerationRequest."""
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    captured: dict = {}
    _capture_generate(monkeypatch, captured)

    result = await investment_report_generate_from_bundle_impl(
        market="kr",
        account_scope="kis_live",
        title="t",
        summary="s",
        kst_date="2026-05-26",
        created_by_profile="test",
        status="draft",
        user_id=42,
    )

    assert result["success"] is True
    assert captured["request"].user_id == 42


@pytest.mark.asyncio
async def test_generate_from_bundle_user_id_defaults_to_mcp_user(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """ROB-352 — omitting user_id now resolves the MCP default (like get_holdings)
    so kis_live portfolios are readable, instead of staying fail-closed. The
    resolved id is surfaced in the response and reaches the request.
    """
    from app.core.config import settings
    from app.mcp_server.tooling.investment_reports_handlers import (
        _default_generator_user_id,
    )

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    captured: dict = {}
    _capture_generate(monkeypatch, captured)

    result = await investment_report_generate_from_bundle_impl(
        market="kr",
        account_scope="kis_live",
        title="t",
        summary="s",
        kst_date="2026-05-26",
        created_by_profile="test",
        status="draft",
    )

    expected = _default_generator_user_id()
    assert result["success"] is True
    assert result["resolved_user_id"] == expected
    assert captured["request"].user_id == expected


@pytest.mark.asyncio
async def test_context_get_draft_policy_advisory_only(
    session: AsyncSession,
) -> None:
    """draft_policy='advisory_only' admits an advisory (HERMES_ADVISOR) draft as
    prior context; the default 'exclude' drops it; a smoke draft stays excluded
    even under 'advisory_only'."""
    # 1. An advisory draft (HERMES_ADVISOR) — the genuine baseline.
    advisory = await investment_report_create_impl(
        items=[_action_item_dict()],
        **_create_kwargs(kst_date="2026-05-18", created_by_profile="HERMES_ADVISOR"),
    )
    # 2. A smoke/test draft (default test profile) — must never be admitted.
    await investment_report_create_impl(
        items=[_action_item_dict("action-2")],
        **_create_kwargs(kst_date="2026-05-17", created_by_profile="t"),
    )

    # 3. Default draft_policy='exclude' drops all drafts.
    ctx_default = await investment_report_context_get_impl(market="kr")
    assert len(ctx_default["prior_reports"]) == 0

    # 4. draft_policy='advisory_only' admits ONLY the advisory draft.
    ctx_advisory = await investment_report_context_get_impl(
        market="kr", draft_policy="advisory_only"
    )
    uuids = {r["report_uuid"] for r in ctx_advisory["prior_reports"]}
    assert uuids == {advisory["report"]["report_uuid"]}

    # 5. An unknown policy (e.g. a hallucinated "all") fails closed to 'exclude'
    #    at the tool boundary — never errors, never over-includes drafts.
    ctx_unknown = await investment_report_context_get_impl(
        market="kr", draft_policy="all"
    )
    assert len(ctx_unknown["prior_reports"]) == 0


@pytest.fixture
def _stub_market_data(monkeypatch):
    """Stub market_data so the watch-recommend tool needs no live network and
    we can assert it touches no broker/order client."""
    from app.mcp_server.tooling import investment_reports_handlers as h
    from app.services.market_data.contracts import Candle

    async def fake_get_quote(symbol, market):
        from app.services.market_data.contracts import Quote

        return Quote(symbol=symbol, market=market, price=100.0, source="stub")

    async def fake_get_ohlcv(symbol, market, period, count, end=None):
        import datetime as _dt

        return [
            Candle(
                symbol=symbol,
                market=market,
                source="stub",
                period="day",
                timestamp=_dt.datetime(2026, 5, d + 1, tzinfo=_dt.UTC),
                open=100.0,
                high=102.0,
                low=98.0,
                close=100.0,
                volume=1.0,
            )
            for d in range(25)
        ]

    monkeypatch.setattr(h.market_data_service, "get_quote", fake_get_quote)
    monkeypatch.setattr(h.market_data_service, "get_ohlcv", fake_get_ohlcv)


@pytest.mark.asyncio
async def test_watch_recommend_dry_run_does_not_persist(
    session: AsyncSession, _stub_market_data
) -> None:
    resp = await investment_watch_recommend_impl(symbol="005930", market="kr")
    assert resp["success"] is True
    assert resp["committed"] is False
    assert resp["recommendation"]["data_state"] == "ok"
    assert resp["recommendation"]["policy_version"] == "v1"


@pytest.mark.asyncio
async def test_watch_recommend_unsupported_market_returns_structured_error(
    session: AsyncSession,
) -> None:
    resp = await investment_watch_recommend_impl(symbol="005930", market="jp")
    assert resp == {"success": False, "error": "unsupported_market", "market": "jp"}


@pytest.mark.asyncio
async def test_watch_recommend_commit_persists_on_watch_only(
    session: AsyncSession, _stub_market_data
) -> None:
    # watch_only item via evidence_snapshot.action_verdict
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    resp = await investment_watch_recommend_impl(
        symbol="005930", market="kr", item_uuid=item_uuid, commit=True, actor="op"
    )
    assert resp["committed"] is True

    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    rec = bundle_post["items"][0]["watch_recommendation"]
    assert rec is not None
    assert rec["data_state"] == "ok"
    assert rec["entry_review_below_price"] is not None


@pytest.mark.asyncio
async def test_watch_recommend_commit_rejected_for_non_watch_verdict(
    session: AsyncSession, _stub_market_data
) -> None:
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {
        "action_verdict": "buy_review"
    }  # not watch_only/limit_wait
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    with pytest.raises(ValueError) as exc:
        await investment_watch_recommend_impl(
            symbol="005930", market="kr", item_uuid=item_uuid, commit=True, actor="op"
        )
    assert "watch_only" in str(exc.value) or "limit_wait" in str(exc.value)


@pytest.mark.asyncio
async def test_watch_recommend_commit_rejected_on_data_gap(
    session: AsyncSession, monkeypatch
) -> None:
    from app.mcp_server.tooling import investment_reports_handlers as h
    from app.services.market_data.contracts import Quote

    async def fake_get_quote(symbol, market):
        return Quote(symbol=symbol, market=market, price=100.0, source="stub")

    async def few_candles(symbol, market, period, count, end=None):
        return []  # data gap

    monkeypatch.setattr(h.market_data_service, "get_quote", fake_get_quote)
    monkeypatch.setattr(h.market_data_service, "get_ohlcv", few_candles)

    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    with pytest.raises(ValueError) as exc:
        await investment_watch_recommend_impl(
            symbol="005930", market="kr", item_uuid=item_uuid, commit=True, actor="op"
        )
    assert "data_gap" in str(exc.value)


# ---------------------------------------------------------------------------
# ROB-455 PR1 — set_status tool + previous_report_uuid as delta baseline
# ---------------------------------------------------------------------------
async def _create_report(**overrides) -> str:
    out = await investment_report_create_impl(**_create_kwargs(**overrides))
    return out["report"]["report_uuid"]


@pytest.mark.asyncio
async def test_set_status_impl_transitions_report(session: AsyncSession) -> None:
    report_uuid = await _create_report()
    out = await investment_report_set_status_impl(
        report_uuid=report_uuid, status="superseded", actor="operator", reason="v2"
    )
    assert out["success"] is True
    assert out["status"] == "superseded"

    fetched = await investment_report_get_impl(report_uuid)
    assert fetched["report"]["status"] == "superseded"


@pytest.mark.asyncio
async def test_set_status_impl_rejects_invalid_status(session: AsyncSession) -> None:
    report_uuid = await _create_report()
    # 'published' is not a lifecycle transition target; 'garbage' is unknown.
    for bad in ("published", "garbage"):
        out = await investment_report_set_status_impl(
            report_uuid=report_uuid, status=bad
        )
        assert out["success"] is False
        assert out["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_set_status_impl_unknown_uuid_returns_not_found(
    session: AsyncSession,
) -> None:
    out = await investment_report_set_status_impl(
        report_uuid=str(uuid.uuid4()), status="expired"
    )
    assert out == {
        "success": False,
        "error": "not_found",
        "report_uuid": out["report_uuid"],
    }


@pytest.mark.asyncio
async def test_delta_get_resolves_previous_report_as_baseline(
    session: AsyncSession, monkeypatch
) -> None:
    # ROB-455 — when use_previous_as_baseline=True, the delta baseline resolves to
    # the report's previous_report_uuid (the report it chains from).
    a_uuid = await _create_report(kst_date="2026-05-18")
    b_uuid = await _create_report(kst_date="2026-05-19", previous_report_uuid=a_uuid)

    captured: dict[str, str] = {}

    class _StubDelta:
        def __init__(self, _db):
            pass

        async def compute_delta(self, report_uuid, **_kw):
            captured["uuid"] = str(report_uuid)
            return {"success": True, "baseline_report_uuid": str(report_uuid)}

    monkeypatch.setattr(
        "app.services.investment_reports.delta_service.DeltaService", _StubDelta
    )

    await investment_report_delta_get_impl(
        report_uuid=b_uuid, use_previous_as_baseline=True
    )
    assert captured["uuid"] == a_uuid  # resolved to the predecessor

    await investment_report_delta_get_impl(report_uuid=b_uuid)
    assert captured["uuid"] == b_uuid  # default: the report itself is the baseline


@pytest.mark.asyncio
async def test_add_items_impl_appends_to_draft_report(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")], **_create_kwargs()
    )
    report_uuid = created["report"]["report_uuid"]

    out = await investment_report_add_items_impl(
        report_uuid=report_uuid,
        items=[_action_item_dict("increment-1") | {"symbol": "000660"}],
        actor="operator",
    )

    assert out["success"] is True
    assert out["inserted_count"] == 1
    assert out["existing_count"] == 0

    fetched = await investment_report_get_impl(report_uuid)
    assert len(fetched["items"]) == 2
    assert {it["metadata"].get("client_item_key") for it in fetched["items"]} == {
        "base-1",
        "increment-1",
    }


@pytest.mark.asyncio
async def test_add_items_impl_replays_duplicate_as_existing(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[], **_create_kwargs(kst_date="2026-05-20")
    )
    report_uuid = created["report"]["report_uuid"]
    item = _action_item_dict("increment-1") | {"symbol": "000660"}

    first = await investment_report_add_items_impl(
        report_uuid=report_uuid, items=[item]
    )
    second = await investment_report_add_items_impl(
        report_uuid=report_uuid, items=[item]
    )

    assert first["success"] is True
    assert first["inserted_count"] == 1
    assert second["success"] is True
    assert second["inserted_count"] == 0
    assert second["existing_count"] == 1

    fetched = await investment_report_get_impl(report_uuid)
    assert len(fetched["items"]) == 1


@pytest.mark.asyncio
async def test_add_items_impl_serializes_concurrent_same_client_key(
    session: AsyncSession, monkeypatch
) -> None:
    created = await investment_report_create_impl(
        items=[], **_create_kwargs(kst_date="2026-05-21")
    )
    report_uuid = created["report"]["report_uuid"]

    original_insert = InvestmentReportIngestionService._insert_item
    first_insert_started = asyncio.Event()
    release_first_insert = asyncio.Event()
    delayed = False

    async def delayed_insert(self, report, item_req):
        nonlocal delayed
        if item_req.client_item_key == "increment-1" and not delayed:
            delayed = True
            first_insert_started.set()
            await release_first_insert.wait()
        return await original_insert(self, report, item_req)

    monkeypatch.setattr(
        InvestmentReportIngestionService, "_insert_item", delayed_insert
    )

    first = asyncio.create_task(
        investment_report_add_items_impl(
            report_uuid=report_uuid,
            items=[_action_item_dict("increment-1") | {"symbol": "000660"}],
        )
    )
    await asyncio.wait_for(first_insert_started.wait(), timeout=2)

    second = asyncio.create_task(
        investment_report_add_items_impl(
            report_uuid=report_uuid,
            items=[_action_item_dict("increment-1") | {"symbol": "005930"}],
        )
    )

    await asyncio.sleep(0.2)
    release_first_insert.set()
    first_out, second_out = await asyncio.gather(first, second)

    assert first_out["success"] is True
    assert second_out["success"] is True
    assert first_out["inserted_count"] + second_out["inserted_count"] == 1
    assert first_out["existing_count"] + second_out["existing_count"] == 1

    fetched = await investment_report_get_impl(report_uuid)
    matching = [
        item
        for item in fetched["items"]
        if item["metadata"].get("client_item_key") == "increment-1"
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_add_items_impl_rejects_published_report(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")], **_create_kwargs()
    )
    report_uuid = created["report"]["report_uuid"]
    await _publish_by_uuid(report_uuid)

    out = await investment_report_add_items_impl(
        report_uuid=report_uuid,
        items=[_action_item_dict("increment-1") | {"symbol": "000660"}],
    )

    assert out["success"] is False
    assert out["error"] == "not_draft"
    assert out["status"] == "published"


@pytest.mark.asyncio
async def test_update_impl_updates_draft_summary_and_snapshots(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")],
        **_create_kwargs(summary="old summary", kst_date="2026-05-21"),
    )
    report_uuid = created["report"]["report_uuid"]

    out = await investment_report_update_impl(
        report_uuid=report_uuid,
        summary="new intraday summary",
        market_snapshot={"kospi": {"last": 2860.12}},
        portfolio_snapshot={"cash": 12345},
        metadata={"source": "intraday_update"},
        actor="operator",
        reason="market moved",
    )

    assert out["success"] is True
    assert out["report"]["summary"] == "new intraday summary"
    assert out["report"]["market_snapshot"] == {"kospi": {"last": 2860.12}}
    assert out["report"]["metadata"]["source"] == "intraday_update"
    assert out["report"]["metadata"]["draft_updates"][-1]["actor"] == "operator"


@pytest.mark.asyncio
async def test_update_impl_rejects_empty_update(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")], **_create_kwargs(kst_date="2026-05-22")
    )

    out = await investment_report_update_impl(
        report_uuid=created["report"]["report_uuid"],
        actor="operator",
    )

    assert out["success"] is False
    assert out["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_activate_watch_attach_recommendation_persists(
    session: AsyncSession, _stub_market_data
) -> None:
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
        attach_recommendation=True,
    )

    assert response["success"] is True
    assert response["recommendation_attached"] is True
    assert response["recommendation_attach_error"] is None

    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    rec = bundle_post["items"][0]["watch_recommendation"]
    assert rec is not None
    assert rec["data_state"] == "ok"


@pytest.mark.asyncio
async def test_activate_watch_attach_recommendation_fails_open(
    session: AsyncSession, monkeypatch
) -> None:
    from app.mcp_server.tooling import investment_reports_handlers as h

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("market data unavailable")

    monkeypatch.setattr(h.market_data_service, "get_quote", _boom)

    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
        attach_recommendation=True,
    )

    assert response["success"] is True
    assert response["alert"]["source_item_uuid"] == watch_uuid
    assert response["recommendation_attached"] is False
    assert "market data unavailable" in response["recommendation_attach_error"]


def test_create_description_mentions_watch_execution_plan_contract() -> None:
    from app.mcp_server.tooling.investment_reports_handlers import (
        ADD_ITEMS_DESCRIPTION,
        CREATE_DESCRIPTION,
    )

    combined = CREATE_DESCRIPTION + " " + ADD_ITEMS_DESCRIPTION
    assert "trigger_checklist" in combined
    assert "string[]" in combined
    assert "max_action" in combined
    assert "amount_krw" in combined
    assert "limit_price_hint" in combined
    assert "ladder_level" in combined
