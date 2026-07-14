from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import approval_message as approval_messages
from app.services.order_proposals.approval_message import (
    build_action_diff,
    build_approval_message,
    build_buying_power_shortfall_text,
    build_callback_data,
    build_loss_cut_confirmation_message,
    parse_callback_data,
)


def _group(**overrides):
    values = {
        "proposal_id": uuid.uuid4(),
        "symbol": "000660",
        "market": "equity_kr",
        "side": "sell",
        "order_type": "limit",
        "thesis": None,
        "strategy": None,
        "valid_until": None,
        "validated_at": None,
        "commit_lease_until": None,
        "source_asof": None,
        "payload_hash": None,
        "approval_nonce": "abc123def4560000",
        "exit_intent": None,
        "exit_reason": None,
        "retrospective_id": None,
        "approval_issue_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "quantity": Decimal("10"),
        "limit_price": Decimal("70000"),
        "approval_hash_digest": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot_payload():
    return {
        "broker_order_id": "old-1",
        "symbol": "000660",
        "side": "sell",
        "order_type": "limit",
        "limit_price": "42000",
        "remaining_quantity": "3.5",
        "status": "open",
        "observed_at": "2026-07-11T00:00:00+00:00",
    }


def _snapshot_rung():
    return _rung(
        rung_index=0,
        quantity=Decimal("3.5"),
        limit_price=Decimal("42000"),
    )


def test_batch_callback_round_trip_and_telegram_limit():
    batch_id = uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")

    data = approval_messages.build_batch_callback_data(
        batch_id=batch_id, nonce="batch_nonce-1"
    )

    assert parse_callback_data(data) == ("ba", "aaaaaaaa", "batch_nonce-1")
    assert len(data.encode()) <= 64


def test_batch_summary_lists_notional_and_account_subtotals():
    batch = SimpleNamespace(
        batch_id=uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111"),
        approval_nonce="batch-nonce",
        expires_at=datetime(2026, 7, 14, 1, 30, tzinfo=UTC),
    )
    proposals = [
        (
            _group(
                symbol="AAPL",
                market="equity_us",
                side="buy",
                account_mode="kis_live",
                broker_account_id="account-1234",
            ),
            [_rung(quantity=Decimal("1"), limit_price=Decimal("100"))],
        ),
        (
            _group(
                symbol="MSFT",
                market="equity_us",
                side="sell",
                account_mode="toss_live",
                broker_account_id=None,
            ),
            [_rung(quantity=Decimal("2"), limit_price=Decimal("50"))],
        ),
    ]

    text, keyboard = approval_messages.build_batch_approval_message(
        batch=batch, proposals=proposals
    )

    assert "AAPL" in text and "MSFT" in text
    assert r"kis\_live ···1234" in text and r"toss\_live" in text
    assert "합계: $200" in text
    assert keyboard["inline_keyboard"][0][0]["text"] == "전체 승인"


def test_batch_summary_omits_market_order_notional_from_totals():
    batch = SimpleNamespace(
        batch_id=uuid.uuid4(),
        approval_nonce="batch-nonce",
        expires_at=datetime(2026, 7, 14, 1, 30, tzinfo=UTC),
    )
    proposals = [
        (
            _group(symbol="AAPL", market="equity_us", account_mode="kis_live"),
            [
                _rung(
                    quantity=Decimal("1"),
                    limit_price=None,
                    notional=Decimal("999"),
                )
            ],
        ),
        (
            _group(symbol="MSFT", market="equity_us", account_mode="kis_live"),
            [_rung(quantity=Decimal("1"), limit_price=Decimal("100"))],
        ),
    ]

    text, _keyboard = approval_messages.build_batch_approval_message(
        batch=batch, proposals=proposals
    )

    assert "합계: $100" in text
    assert "$999" not in text
    assert "$1,099" not in text


def test_batch_result_groups_each_member_outcome():
    groups = [
        _group(symbol="AAPL"),
        _group(symbol="MSFT"),
        _group(symbol="NVDA"),
        _group(symbol="AMZN"),
    ]

    text = approval_messages.build_batch_result_message(
        proposals=[(group, [_rung()]) for group in groups],
        results=[
            {
                "proposal_id": str(groups[0].proposal_id),
                "status": "approved",
                "rung_results": ["submitted_resting"],
            },
            {
                "proposal_id": str(groups[1].proposal_id),
                "status": "needs_reconfirm",
            },
            {
                "proposal_id": str(groups[2].proposal_id),
                "status": "skipped",
                "reason": "nonce_replay",
            },
            {
                "proposal_id": str(groups[3].proposal_id),
                "status": "failed",
                "reason": "broker unavailable",
                "rung_results": ["unverified"],
            },
        ],
    )

    assert "승인 완료" in text and "AAPL" in text
    assert "재확인 필요" in text and "MSFT" in text
    assert "제외/건너뜀" in text and "NVDA" in text
    assert "실패" in text and "AMZN" in text
    assert "#1 주문 유지(대기)" in text
    assert "#1 확인 불가(수동 확인 필요)" in text


@pytest.mark.unit
def test_loss_cut_approval_message_shows_reason_and_retrospective():
    group = _group(
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-800",
    )
    text, _keyboard = build_approval_message(group=group, rungs=[_rung()])
    assert "손절 근거" in text
    assert r"stop\_loss" in text
    assert "#42" in text
    assert "ROB-800" not in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            {
                "reason": "insufficient_buying_power",
                "currency": "KRW",
                "available": "400000",
                "required": "1070300",
                "shortfall": "670300",
            },
            ("매수가능 400,000원 / 필요 1,070,300원 → 부족 670,300원 — 입금 후 재승인"),
        ),
        (
            {
                "reason": "insufficient_buying_power",
                "currency": "USD",
                "available": "100",
                "required": "123.45",
                "shortfall": "23.45",
            },
            ("매수가능 $100.00 / 필요 $123.45 → 부족 $23.45 — 입금 후 재승인"),
        ),
    ],
)
def test_buying_power_shortfall_text_formats_currency(detail, expected):
    assert build_buying_power_shortfall_text(detail) == expected


@pytest.mark.unit
def test_buying_power_reconfirm_message_shows_shortfall_without_fake_diff():
    detail = {
        "reason": "insufficient_buying_power",
        "currency": "KRW",
        "available": "400000",
        "required": "1070300",
        "shortfall": "670300",
    }

    text, keyboard = build_approval_message(
        group=_group(side="buy"),
        rungs=[_rung()],
        diff=detail,
    )

    assert "매수가능 금액 부족" in text
    assert "매수가능 400,000원 / 필요 1,070,300원" in text
    assert "입금 후 재승인" in text
    assert "변경 전" not in text
    assert [button["text"] for button in keyboard["inline_keyboard"][0]] == [
        "✅ 승인",
        "❌ 거부",
    ]


@pytest.mark.unit
def test_callback_data_roundtrip_and_length():
    proposal_id = uuid.uuid4()

    data = build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce="abc123def4560000",
    )

    assert len(data.encode("utf-8")) <= 64
    action, proposal_short, nonce = parse_callback_data(data)
    assert action == "op"
    assert proposal_short == str(proposal_id)[:8]
    assert nonce == "abc123def4560000"


@pytest.mark.unit
def test_loss_cut_confirmation_callback_and_summary():
    group = _group(
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="operator note: desk A",
        approval_nonce="second-step-nonce",
    )
    text, keyboard = build_loss_cut_confirmation_message(
        group=group,
        rungs=[_rung(quantity=Decimal("3"), limit_price=Decimal("99"))],
        evidence={
            "rungs": [
                {
                    "rung_index": 0,
                    "current_price": "100",
                    "avg_buy_price": "200",
                    "loss_pct": "-50.00",
                    "loss_cut_slip_band": "98",
                }
            ],
            "retrospective_id": 42,
            "lesson_excerpt": "손절 기준을 늦추지 않는다",
        },
    )

    assert "손절 확인" in text
    assert "3주" in text
    assert "99" in text
    assert "100" in text
    assert "-50.00%" in text
    assert "#42" in text
    assert "손절 기준을 늦추지 않는다" in text
    assert "승인 감사 메모" in text
    assert "operator note: desk A" in text
    assert "98" in text
    button = keyboard["inline_keyboard"][0][0]
    assert button["text"] == "⚠️ 손절 확인"
    action, proposal_short, nonce = parse_callback_data(button["callback_data"])
    assert (action, proposal_short, nonce) == (
        "lc",
        str(group.proposal_id)[:8],
        "second-step-nonce",
    )


@pytest.mark.unit
def test_callback_builder_rejects_invalid_action_and_oversized_data():
    proposal_id = uuid.uuid4()

    with pytest.raises(ValueError, match="action"):
        build_callback_data(
            action="approve",
            proposal_id=proposal_id,
            nonce="abc123def4560000",
        )
    with pytest.raises(ValueError, match="64 bytes"):
        build_callback_data(
            action="op",
            proposal_id=proposal_id,
            nonce="a" * 53,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "data",
    [
        "",
        "op:deadbeef",
        "op:deadbeef:nonce:extra",
        "approve:deadbeef:nonce",
        "op:deadbee:nonce",
        "op:deadbeeg:nonce",
        "op:deadbeef:",
        "op:deadbeef:bad nonce",
        f"op:deadbeef:{'a' * 53}",
    ],
)
def test_callback_parser_rejects_malformed_data(data):
    with pytest.raises(ValueError):
        parse_callback_data(data)


@pytest.mark.unit
def test_message_includes_times_cash_and_reconfirm_diff_without_secrets():
    proposal_id = uuid.UUID("12345678-1234-5678-9abc-123456789abc")
    payload_hash = "payload-secret-digest-0123456789"
    approval_hash = "approval-secret-digest-9876543210"
    nonce = "abc123def4560000"
    group = SimpleNamespace(
        proposal_id=proposal_id,
        symbol="000660",
        market="equity_kr",
        side="buy",
        order_type="limit",
        thesis="support bounce",
        strategy="ladder",
        valid_until=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
        validated_at=datetime(2026, 7, 10, 10, 57, tzinfo=UTC),
        commit_lease_until=datetime(2026, 7, 10, 10, 58, tzinfo=UTC),
        source_asof={"resting_deadline": "2026-07-10T11:00:00+00:00"},
        payload_hash=payload_hash,
        approval_nonce=nonce,
    )
    rungs = [
        SimpleNamespace(
            rung_index=1,
            quantity=Decimal("5.000000000000"),
            limit_price=Decimal("68000.000000000000"),
            approval_hash_digest="second-rung-secret",
        ),
        SimpleNamespace(
            rung_index=0,
            quantity=Decimal("10.000000000000"),
            limit_price=Decimal("70000.000000000000"),
            approval_hash_digest=approval_hash,
        ),
    ]
    cash_stress = {
        "available_cash": Decimal("5000000.0000"),
        "required_cash": Decimal("700000.0000"),
        "remaining_cash": Decimal("4300000.0000"),
        "utilization_pct": Decimal("14.00"),
        "payload_hash": payload_hash,
    }
    diff = {
        "before": {
            "quantity": Decimal("10.0000"),
            "limit_price": Decimal("70000.0000"),
            "approval_hash": approval_hash,
        },
        "after": {
            "quantity": Decimal("9.0000"),
            "limit_price": Decimal("70500.0000"),
        },
    }

    text, inline_keyboard = build_approval_message(
        group=group,
        rungs=rungs,
        cash_stress=cash_stress,
        diff=diff,
    )

    assert "000660" in text
    assert "equity_kr / buy / limit" in text
    assert text.index("#1: 10주 × ₩70,000") < text.index("#2: 5주 × ₩68,000")
    assert "투자 논지: support bounce" in text
    assert "전략: ladder" in text
    assert "유효기간: ~20:00 KST (2026-07-10)" in text
    assert "검증시각: 19:57 KST (2026-07-10)" in text
    assert "제출 임대: ~19:58 KST (2026-07-10)" in text
    assert "주문 유지기한: ~20:00 KST (2026-07-10)" in text
    assert "가용현금: ₩5,000,000" in text
    assert "필요현금: ₩700,000" in text
    assert "잔여현금: ₩4,300,000" in text
    assert "사용률: 14%" in text
    assert "변경 전: 수량 10 / 가격 ₩70,000" in text
    assert "변경 후: 수량 9 / 가격 ₩70,500" in text

    assert payload_hash not in text
    assert approval_hash not in text
    assert nonce not in text
    assert "second-rung-secret" not in text
    assert "payload_hash" not in text
    assert "approval_hash" not in text
    assert "nonce" not in text
    assert "digest" not in text
    assert inline_keyboard == {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 승인",
                    "callback_data": "op:12345678:abc123def4560000",
                },
                {
                    "text": "❌ 거부",
                    "callback_data": "dn:12345678:abc123def4560000",
                },
            ]
        ]
    }


@pytest.mark.unit
def test_replace_message_renders_target_before_new_rung_after():
    group = _group(
        action="replace",
        target_broker_order_id="old-1",
        source_asof={"target_order_snapshot": _snapshot_payload()},
    )

    text, _ = build_approval_message(
        group=group,
        rungs=[_rung(quantity=Decimal("3.5"), limit_price=Decimal("43000"))],
    )

    assert "replace" in text
    assert "old-1" in text
    assert "변경 전: 수량 3.5 / 가격 ₩42,000" in text
    assert "변경 후: 수량 3.5 / 가격 ₩43,000" in text
    assert "재확인" not in text


@pytest.mark.unit
def test_cancel_message_renders_zero_remaining_after():
    group = _group(
        action="cancel",
        target_broker_order_id="old-1",
        source_asof={"target_order_snapshot": _snapshot_payload()},
    )

    text, _ = build_approval_message(group=group, rungs=[_snapshot_rung()])

    assert "cancel" in text
    assert "old-1" in text
    assert "변경 전: 수량 3.5 / 가격 ₩42,000" in text
    assert "변경 후: 수량 0" in text
    assert "가격 ₩42,000" in text
    assert "재확인" not in text


@pytest.mark.unit
def test_build_action_diff_returns_none_for_place():
    group = _group(source_asof={"target_order_snapshot": _snapshot_payload()})

    assert build_action_diff(group=group, rungs=[_snapshot_rung()]) is None


@pytest.mark.unit
def test_build_action_diff_returns_none_for_multiple_rungs():
    group = _group(
        action="replace",
        source_asof={"target_order_snapshot": _snapshot_payload()},
    )

    assert (
        build_action_diff(group=group, rungs=[_snapshot_rung(), _snapshot_rung()])
        is None
    )


@pytest.mark.unit
def test_build_action_diff_returns_none_when_snapshot_absent():
    group = _group(action="replace", source_asof={})

    assert build_action_diff(group=group, rungs=[_snapshot_rung()]) is None


@pytest.mark.unit
def test_build_action_diff_returns_none_when_snapshot_not_mapping():
    group = _group(
        action="replace",
        source_asof={"target_order_snapshot": ["not", "a", "mapping"]},
    )

    assert build_action_diff(group=group, rungs=[_snapshot_rung()]) is None


@pytest.mark.unit
def test_message_escapes_markdown_sensitive_target_broker_order_id():
    group = _group(
        action="replace",
        target_broker_order_id=r"old`\id*[]",
        source_asof={"target_order_snapshot": _snapshot_payload()},
    )

    text, _ = build_approval_message(group=group, rungs=[_snapshot_rung()])

    assert r"- 대상 주문 ID: `old\`\\id*[]`" in text


@pytest.mark.unit
def test_message_omits_nested_and_non_numeric_sensitive_values():
    group = SimpleNamespace(
        proposal_id=uuid.uuid4(),
        symbol="000660",
        market="equity_kr",
        side="buy",
        order_type="limit",
        thesis=None,
        strategy=None,
        valid_until=None,
        validated_at=None,
        commit_lease_until=None,
        source_asof=None,
        payload_hash=None,
        approval_nonce="abc123def4560000",
    )
    rung = SimpleNamespace(
        rung_index=0,
        quantity=Decimal("10"),
        limit_price=Decimal("70000"),
        approval_hash_digest=None,
    )
    sensitive_values = {
        "nested-payload-secret",
        "nested-nonce-secret",
        "known-cash-field-secret",
        "nested-digest-secret",
        "known-diff-field-secret",
        "nested-approval-secret",
    }
    cash_stress = {
        "available_cash": Decimal("5000000"),
        "required_cash": {"approval_hash": "known-cash-field-secret"},
        "details": {
            "payload_hash": "nested-payload-secret",
            "items": [{"nonce": "nested-nonce-secret"}],
        },
    }
    diff = {
        "before": {
            "quantity": Decimal("10"),
            "limit_price": Decimal("70000"),
            "details": {"digest": "nested-digest-secret"},
        },
        "after": {
            "quantity": {"nonce": "known-diff-field-secret"},
            "limit_price": Decimal("70500"),
            "metadata": [{"approval_hash": "nested-approval-secret"}],
        },
    }

    text, _ = build_approval_message(
        group=group,
        rungs=[rung],
        cash_stress=cash_stress,
        diff=diff,
    )

    assert "가용현금: ₩5,000,000" in text
    assert "변경 전: 수량 10 / 가격 ₩70,000" in text
    assert "변경 후: 가격 ₩70,500" in text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in text
    for sensitive_key in ("payload_hash", "approval_hash", "nonce", "digest"):
        assert sensitive_key not in text


@pytest.mark.unit
def test_message_formats_optional_market_order_fields_stably():
    group = SimpleNamespace(
        proposal_id=uuid.uuid4(),
        symbol="BTC/KRW",
        market="crypto",
        side="buy",
        order_type="market",
        thesis=None,
        strategy=None,
        valid_until=None,
        validated_at=None,
        commit_lease_until=None,
        source_asof=None,
        payload_hash=None,
        approval_nonce="abc123def4560000",
    )
    rung = SimpleNamespace(
        rung_index=0,
        quantity=Decimal("0.010000000000"),
        limit_price=None,
        approval_hash_digest=None,
    )

    text, _ = build_approval_message(group=group, rungs=[rung])

    assert "#1: 0.01 × 시장가" in text
    assert "투자 논지: 미기재" in text
    assert "전략: 미기재" in text
    assert "*시간*" not in text
    assert "*현금 스트레스*" not in text
    assert "*재확인 변경사항*" not in text


@pytest.mark.unit
def test_message_escapes_inline_code_delimiters():
    group = SimpleNamespace(
        proposal_id=uuid.uuid4(),
        symbol=r"A`\B",
        market=r"equity`\kr",
        side=r"b`\uy",
        order_type=r"li`\mit",
        thesis=None,
        strategy=None,
        valid_until=None,
        validated_at=None,
        commit_lease_until=None,
        source_asof=None,
        payload_hash=None,
        approval_nonce="abc123def4560000",
    )

    text, _ = build_approval_message(group=group, rungs=[])

    assert r"- 종목: `A\`\\B`" in text
    assert r"- 시장/방향/유형: `equity\`\\kr / b\`\\uy / li\`\\mit`" in text


@pytest.mark.unit
def test_message_requires_group_approval_nonce():
    group = SimpleNamespace(
        proposal_id=uuid.uuid4(),
        symbol="000660",
        market="equity_kr",
        side="buy",
        order_type="limit",
        thesis=None,
        strategy=None,
        valid_until=None,
        validated_at=None,
        commit_lease_until=None,
        source_asof=None,
        payload_hash=None,
        approval_nonce=None,
    )

    with pytest.raises(ValueError, match="approval_nonce"):
        build_approval_message(group=group, rungs=[])
