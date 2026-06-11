"""ROB-458 — investment_report_create 계약/일괄검증 핸들러 테스트.

검증 단계는 DB 세션을 열기 전에 단락(short-circuit)되므로 DB 픽스처 불필요.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h

pytestmark = pytest.mark.unit


def test_validate_report_items_collects_all_missing_fields_at_once():
    # client_item_key + intent + rationale 누락 — 세 위반이 한 응답에 모여야 한다.
    items = [{"item_kind": "action"}]
    validated, error = h._validate_report_items(items)
    assert validated == []
    assert error is not None
    assert error["error"] == "invalid_items"
    fields = {e["field"] for e in error["item_errors"][0]["errors"]}
    assert {"client_item_key", "intent", "rationale"} <= fields


def test_validate_report_items_reports_bad_enum_with_enum_block():
    items = [
        {
            "client_item_key": "k1",
            "item_kind": "action",
            "intent": "not_a_real_intent",
            "rationale": "r",
        }
    ]
    _validated, error = h._validate_report_items(items)
    assert error is not None
    assert "intent" in str(error["item_errors"][0]["errors"])
    assert error["enums"]["item_kind"] == ["action", "watch", "risk"]


def test_validate_report_items_non_dict_does_not_crash():
    _validated, error = h._validate_report_items(["not-a-dict"])
    assert error is not None
    assert error["item_errors"][0]["index"] == 0
    assert "object" in str(error["item_errors"][0]["errors"])


def test_validate_report_items_happy_path_returns_items_and_no_error():
    items = [
        {
            "client_item_key": "k1",
            "item_kind": "action",
            "intent": "buy_review",
            "rationale": "r",
        }
    ]
    validated, error = h._validate_report_items(items)
    assert error is None
    assert len(validated) == 1
    assert validated[0].client_item_key == "k1"


def test_validate_report_items_only_flags_the_bad_index():
    items = [
        {
            "client_item_key": "ok",
            "item_kind": "action",
            "intent": "buy_review",
            "rationale": "r",
        },
        {"item_kind": "action"},  # bad
    ]
    _validated, error = h._validate_report_items(items)
    assert error is not None
    assert [e["index"] for e in error["item_errors"]] == [1]


def _kwargs(**overrides):
    base = {
        "report_type": "snapshot_backed_advisory_v1",
        "market": "us",
        "summary": "s",
        "created_by_profile": "claude_code",
        "title": "t",
        "kst_date": "2026-06-09",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_invalid_item_returns_structured_error_without_db():
    # client_item_key 누락 — DB 세션을 열기 전에 구조화 에러로 단락되어야 한다.
    res = await h.investment_report_create_impl(
        **_kwargs(
            items=[{"item_kind": "action", "intent": "buy_review", "rationale": "r"}]
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert res["item_errors"][0]["index"] == 0
    assert "client_item_key" in str(res["item_errors"][0]["errors"])


@pytest.mark.asyncio
async def test_create_invalid_enum_names_the_field():
    res = await h.investment_report_create_impl(
        **_kwargs(
            items=[
                {
                    "client_item_key": "k1",
                    "item_kind": "action",
                    "intent": "not_a_real_intent",
                    "rationale": "r",
                }
            ]
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert "intent" in str(res["item_errors"][0]["errors"])


def test_create_tool_description_documents_item_contract():
    desc = h.CREATE_DESCRIPTION
    assert "client_item_key" in desc
    assert "item_kind" in desc
    assert "action|watch|risk" in desc
    # item_kind vs target_kind 혼동 방지 문구가 반드시 노출되어야 한다(ROB-458 핵심).
    assert "target_kind" in desc
    assert "NOT item_kind" in desc


def test_create_description_documents_structured_evidence_and_chaining():
    # ROB-459 — 이제 main에 evidence 필드(P1)와 CLAUDE_ADVISOR 체이닝(P3)이 있으므로
    # create 도구 description이 둘을 정직하게 광고해야 한다.
    desc = h.CREATE_DESCRIPTION
    assert "evidence" in desc
    assert "source" in desc
    assert "CLAUDE_ADVISOR" in desc


def test_validate_report_items_rejects_unknown_keys():
    _validated, error = h._validate_report_items(
        [
            {
                "client_item_key": "k1",
                "item_kind": "action",
                "intent": "buy_review",
                "rationale": "r",
                "entry_price": 100,
            }
        ]
    )
    assert error is not None
    assert error["error"] == "invalid_items"
    assert error["item_errors"][0]["errors"][0]["field"] == "entry_price"


def test_validate_report_items_rejects_legacy_watch_bad_max_action():
    _validated, error = h._validate_report_items(
        [
            {
                "client_item_key": "legacy-watch",
                "item_kind": "watch",
                "intent": "buy_review",
                "rationale": "r",
                "symbol": "005930",
                "watch_condition": {
                    "metric": "price",
                    "operator": "below",
                    "threshold": "5",
                },
                "valid_until": "2026-12-31T00:00:00Z",
                "max_action": {"side": "buy", "account_mode": "kis_mock"},
            }
        ]
    )

    assert error is not None
    assert error["error"] == "invalid_items"
    assert error["item_errors"][0]["index"] == 0
    assert "max_action" in str(error["item_errors"][0]["errors"])
    assert "quantity or notional" in str(error["item_errors"][0]["errors"])


def test_create_description_documents_trade_plan_and_unknown_key_policy():
    desc = h.CREATE_DESCRIPTION
    assert "entry_plan" in desc
    assert "stop_loss" in desc
    assert "target_price" in desc
    assert "linked_order_ids" in desc
    assert "Unknown item keys are rejected" in desc
    assert "metadata" in desc
    assert "item_evidence_lite" in desc
    assert "evidence[]" in desc


def test_create_description_documents_required_max_action_account_mode():
    combined = h.CREATE_DESCRIPTION + " " + h.ADD_ITEMS_DESCRIPTION
    assert "account_mode is required" in combined
    assert "quantity or notional" in combined
    assert "trigger_checklist" in combined
    assert "planned_action" in combined
