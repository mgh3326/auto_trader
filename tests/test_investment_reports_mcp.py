"""ROB-265 Plan 3 — MCP handler tests.

Each tool's ``*_impl`` is called directly (matches the legacy
``test_analysis_report_workflow.py`` style). The handlers open their
own ``AsyncSessionLocal`` against the same test_db that the ``session``
fixture manages; the fixture's per-test TRUNCATE keeps state clean.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.investment_reports_handlers import (
    INVESTMENT_REPORT_TOOL_NAMES,
    investment_report_activate_watch_impl,
    investment_report_context_get_impl,
    investment_report_create_impl,
    investment_report_decide_item_impl,
    investment_report_get_impl,
    investment_report_list_impl,
)
from tests._investment_reports_helpers import future_datetime


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


def test_tool_names_are_exactly_six() -> None:
    assert INVESTMENT_REPORT_TOOL_NAMES == {
        "investment_report_create",
        "investment_report_list",
        "investment_report_get",
        "investment_report_decide_item",
        "investment_report_activate_watch",
        "investment_report_context_get",
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
        await investment_report_create_impl(
            items=[_action_item_dict()],
            **_create_kwargs(kst_date=f"2026-05-{i + 1:02d}"),
        )
    ctx = await investment_report_context_get_impl(market="kr", n_prior=50)
    assert len(ctx["prior_reports"]) == 10
