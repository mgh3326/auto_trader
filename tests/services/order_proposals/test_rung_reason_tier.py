"""ROB-s257 E-2 — observation-only rung void-reason taxonomy contracts."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling.order_proposal_tools import _rung_dict
from app.models.order_proposals import OrderProposalRung
from app.models.rung_reason_vocabulary import (
    RUNG_VOID_REASON_GROUPS,
    UNCLASSIFIED_VOID_REASON_GROUP,
    project_rung_void_reason_group,
    sql_in_list,
    validate_rung_void_reason_group,
)
from app.services.brokers.kis import order_throttle
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.rung_reason import classify_rung_void_reason
from app.services.order_proposals.service import RungInput

_REPO = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO / "alembic/versions/20260824_rob_s257_rung_reason_group.py"
_SERVICE = _REPO / "app/services/order_proposals/service.py"
_CLASSIFIER = _REPO / "app/services/order_proposals/rung_reason.py"


@pytest.mark.unit
def test_leaf_vocabulary_drives_model_check_and_validator() -> None:
    leaf = _REPO / "app/models/rung_reason_vocabulary.py"
    tree = ast.parse(leaf.read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    imports += [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    ]
    assert imports == []

    column = OrderProposalRung.__table__.columns["void_reason_group"]
    assert column.nullable is True
    check = next(
        constraint
        for constraint in OrderProposalRung.__table__.constraints
        if getattr(constraint, "name", None)
        == "ck_order_proposal_rungs_order_proposal_rungs_void_reason_group"
    )
    assert set(re.findall(r"'([^']+)'", str(check.sqltext))) == set(
        RUNG_VOID_REASON_GROUPS
    )
    assert sql_in_list(RUNG_VOID_REASON_GROUPS) in str(check.sqltext)

    for group in RUNG_VOID_REASON_GROUPS:
        assert validate_rung_void_reason_group(group) == group
    with pytest.raises(ValueError, match="invalid rung void reason group"):
        validate_rung_void_reason_group("new_group_added_by_accident")


@pytest.mark.unit
def test_additive_migration_derives_same_check_and_has_no_backfill() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "op.add_column" in source
    assert "nullable=True" in source
    assert "op.create_check_constraint" in source
    assert "void_reason_group" in source
    assert not re.search(r"\b(insert\s+into|update\s+|delete\s+from)\b", source, re.I)
    assert "no historical rows are backfilled" in source.lower()

    namespace: dict[str, object] = {}
    exec(compile(source, str(_MIGRATION), "exec"), namespace)  # noqa: S102
    assert namespace["_CHECK_SQL"] == (
        "void_reason_group IS NULL OR void_reason_group IN ("
        + sql_in_list(RUNG_VOID_REASON_GROUPS)
        + ")"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "EGW00201 초당 거래건수를 초과하였습니다.",
            "provider_throttle",
        ),
        (
            "동일 주문이 오늘 이미 전송되어 중복 전송을 차단했습니다 "
            "(duplicate order intent).",
            "duplicate_pending_intent",
        ),
        (
            "duplicate mock mirror intent; duplicate order intent",
            "duplicate_pending_intent",
        ),
        ("broker_rejected", "broker_rejection"),
        ("insufficient balance", "policy_guard"),
        ("expired", "cancelled_or_expired"),
        ("a newly invented reason", "unclassified"),
    ],
)
def test_known_reason_groups_and_unknown_fallback(reason: str, expected: str) -> None:
    assert classify_rung_void_reason(reason) == expected


@pytest.mark.unit
def test_throttle_group_delegates_to_existing_provider_predicate(monkeypatch) -> None:
    calls: list[tuple[object, object]] = []

    def fake_is_provider_throttle_reject(msg_cd: object, msg1: object) -> bool:
        calls.append((msg_cd, msg1))
        return True

    monkeypatch.setattr(
        order_throttle,
        "is_provider_throttle_reject",
        fake_is_provider_throttle_reject,
    )
    # The classifier imported the same function object at module load. Patch
    # that binding too, so this test proves the call boundary, not a duplicate
    # local implementation.
    import app.services.order_proposals.rung_reason as rung_reason

    monkeypatch.setattr(
        rung_reason, "is_provider_throttle_reject", fake_is_provider_throttle_reject
    )

    assert classify_rung_void_reason("EGW00201") == "provider_throttle"
    assert calls == [("EGW00201", "egw00201")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason",
    [
        "duplicate order intnt",
        "new policy guard reason",
        "provider throttle-ish failure",
    ],
)
def test_near_miss_or_new_text_does_not_silently_join_a_known_group(
    reason: str,
) -> None:
    assert classify_rung_void_reason(reason) == UNCLASSIFIED_VOID_REASON_GROUP


@pytest.mark.unit
def test_legacy_or_invalid_stored_group_projects_unclassified() -> None:
    assert (
        project_rung_void_reason_group(
            void_reason="legacy free text", stored_group=None
        )
        == UNCLASSIFIED_VOID_REASON_GROUP
    )
    assert (
        project_rung_void_reason_group(
            void_reason="legacy free text", stored_group="not_a_group"
        )
        == UNCLASSIFIED_VOID_REASON_GROUP
    )
    assert project_rung_void_reason_group(void_reason=None, stored_group=None) is None


@pytest.mark.unit
def test_read_projection_preserves_free_text_and_adds_group() -> None:
    rung = SimpleNamespace(
        rung_index=0,
        side="sell",
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        notional=None,
        state="rejected",
        void_reason="EGW00201 초당 거래건수를 초과하였습니다.",
        void_reason_group="provider_throttle",
        broker_order_id=None,
        correlation_id=None,
    )
    projected = _rung_dict(rung)
    assert projected["void_reason"] == rung.void_reason
    assert projected["void_reason_group"] == "provider_throttle"


@pytest.mark.asyncio
async def test_service_records_group_without_changing_rejection_state_or_time(
    db_session,
) -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="S257E2-THROTTLE",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="test",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
    )
    await service.record_rejected(
        group.proposal_id,
        0,
        reason="EGW00201 초당 거래건수를 초과하였습니다.",
        now=now,
    )
    refreshed, rungs = await service.get_proposal(group.proposal_id)

    assert refreshed.lifecycle_state == "rejected"
    assert rungs[0].state == "rejected"
    assert rungs[0].updated_at == now
    assert rungs[0].void_reason == "EGW00201 초당 거래건수를 초과하였습니다."
    assert rungs[0].void_reason_group == "provider_throttle"


@pytest.mark.asyncio
async def test_aug_20_three_failures_separate_throttle_and_duplicate_groups(
    db_session,
) -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    service = OrderProposalsService(db_session)
    reasons = (
        "EGW00201 초당 거래건수를 초과하였습니다.",
        "동일 주문이 오늘 이미 전송되어 중복 전송을 차단했습니다 (duplicate order intent).",
        "duplicate mock mirror intent; duplicate order intent",
    )
    groups = []
    for index, reason in enumerate(reasons):
        group = await service.create_proposal(
            symbol=f"S257E2-{index}",
            market="equity_kr",
            account_mode="kis_live",
            side="sell",
            order_type="limit",
            proposer="test",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
        )
        await service.record_rejected(
            group.proposal_id,
            0,
            reason=reason,
            now=now,
        )
        groups.append(group)

    materialized = [
        (await service.get_proposal(group.proposal_id))[1][0] for group in groups
    ]
    assert [rung.state for rung in materialized] == ["rejected"] * 3
    assert [rung.void_reason_group for rung in materialized] == [
        "provider_throttle",
        "duplicate_pending_intent",
        "duplicate_pending_intent",
    ]
    assert [rung.void_reason for rung in materialized] == list(reasons)


@pytest.mark.unit
def test_observation_layer_has_no_second_send_or_state_decision_surface() -> None:
    classifier_source = _CLASSIFIER.read_text(encoding="utf-8").lower()
    assert not re.search(r"\b(retry|defer|redispatch)\b", classifier_source)

    service_source = _SERVICE.read_text(encoding="utf-8")
    assert "sm.assert_rung_transition(rung.state, new_state)" in service_source
    assert "sm.assert_rung_transition(rung.state, target_state)" in service_source
    assert 'new_state="rejected"' in service_source
    assert 'new_state="unverified"' in service_source
