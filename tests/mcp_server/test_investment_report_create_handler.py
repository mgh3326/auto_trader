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
