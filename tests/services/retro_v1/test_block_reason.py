"""hk 5610: ``block_reason`` on proposal and rung rows (why not submitted)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.services.retro_v1 import read_retro_day
from app.services.retro_v1.block_reason import (
    BLOCK_REASONS,
    block_fields,
    proposal_block_reason,
    rung_block_reason,
)
from tests.services.retro_v1.support import (
    kr_day_session,
    ns,
    proposal,
    rung,
    validate,
)

pytestmark = pytest.mark.unit

DAY = date(2026, 1, 5)
CREATED = datetime(2026, 1, 5, 1, 0, tzinfo=UTC)
CONTRACT = Path(__file__).resolve().parents[3] / "contracts/retro/v1/openapi.yaml"
#: Text that must never reach a response: stored reasons, failure details,
#: broker messages.
SECRET = "RAW-BROKER-MESSAGE-항목-secret"


async def rows(proposal_kw: dict[str, Any], rung_kw: dict[str, Any]) -> dict:
    session = kr_day_session(
        **{
            "FROM review.order_proposals ": [
                proposal(10, "005930", CREATED, **proposal_kw)
            ],
            "FROM review.order_proposal_rungs": [rung(100, 10, **rung_kw)],
            "FROM review.kis_live_order_ledger": [],
            "FROM review.order_send_intents": [],
        }
    )
    response = await read_retro_day(session, market="kr", date=DAY)
    validate(response, "RetroDayResponse")
    return {
        r["stage"]: r for r in response["data"] if r["stage"] in {"proposal", "rung"}
    }


def block(row: dict) -> tuple[str | None, str | None]:
    return row.get("block_reason"), row.get("block_reason_basis")


# --------------------------------------------------------------------------
# each vocabulary value, through the real chain rows
# --------------------------------------------------------------------------
HUMAN_CARD = {
    "approval_dispatch_state": "sent_current",
    "approval_dispatch_card_kind": "manual",
}
REJECTIONS = {
    "auto_approve_rejections": [
        {"evaluated_at": CREATED.isoformat(), "rungs": [{"rung_index": 0}]}
    ]
}


def window(code: str) -> dict[str, Any]:
    return {
        "lifecycle_state": "proposed",
        "approval_dispatch_state": "failed",
        "approval_dispatch_failure_code": f"{code}/{SECRET}",
    }


CASES: list[tuple[str, dict, dict, tuple[str, str] | None, tuple[str, str] | None]] = [
    # (name, proposal columns, rung columns, expected proposal, expected rung)
    (
        "awaiting_human_approval",
        {"lifecycle_state": "proposed", **HUMAN_CARD},
        {"state": "pending_approval", "broker_order_id": None},
        ("awaiting_human_approval", "recorded"),
        ("awaiting_human_approval", "recorded"),
    ),
    (
        "auto_approve_demoted",
        {"lifecycle_state": "proposed", "source_asof": REJECTIONS, **HUMAN_CARD},
        {"state": "pending_approval", "broker_order_id": None},
        ("auto_approve_demoted", "recorded"),
        ("auto_approve_demoted", "recorded"),
    ),
    (
        "deferred_session_closed",
        window("DEFER_SESSION_CLOSED"),
        {"state": "pending_approval", "broker_order_id": None},
        ("deferred_session_closed", "recorded"),
        ("deferred_session_closed", "recorded"),
    ),
    (
        "no_executable_window",
        window("NO_EXECUTABLE_WINDOW"),
        {"state": "needs_reconfirm", "broker_order_id": None},
        ("no_executable_window", "recorded"),
        ("no_executable_window", "recorded"),
    ),
    (
        "calendar_unknown",
        window("CALENDAR_UNKNOWN"),
        {"state": "pending_approval", "broker_order_id": None},
        ("calendar_unknown", "recorded"),
        ("calendar_unknown", "recorded"),
    ),
    (
        "invalid_valid_until",
        window("INVALID_VALID_UNTIL"),
        {"state": "pending_approval", "broker_order_id": None},
        ("invalid_valid_until", "recorded"),
        ("invalid_valid_until", "recorded"),
    ),
    (
        "expired_valid_until recorded",
        {
            "lifecycle_state": "expired",
            "void_reason": "expired_valid_until_night_sweep",
        },
        {
            "state": "expired",
            "void_reason": "expired_valid_until_night_sweep",
            "broker_order_id": None,
        },
        ("expired_valid_until", "recorded"),
        ("expired_valid_until", "recorded"),
    ),
    (
        "expired_valid_until inference (never sent)",
        {"lifecycle_state": "expired"},
        {"state": "expired", "broker_order_id": None, "idempotency_key": None},
        ("expired_valid_until", "inference"),
        ("expired_valid_until", "inference"),
    ),
    (
        "voided",
        {"lifecycle_state": "voided", "void_reason": SECRET},
        {"state": "voided", "void_reason": SECRET},
        ("voided", "recorded"),
        ("voided", "recorded"),
    ),
    (
        "superseded",
        {"lifecycle_state": "superseded"},
        {"state": "superseded"},
        ("superseded", "recorded"),
        ("superseded", "recorded"),
    ),
    (
        "denied_by_operator",
        {"lifecycle_state": "rejected", "void_reason": "telegram_deny"},
        {"state": "rejected", "void_reason": "operator_denied"},
        ("denied_by_operator", "recorded"),
        ("denied_by_operator", "recorded"),
    ),
    (
        "rejected_policy_guard",
        {"lifecycle_state": "rejected"},
        {
            "state": "rejected",
            "void_reason": SECRET,
            "void_reason_group": "policy_guard",
        },
        None,
        ("rejected_policy_guard", "recorded"),
    ),
    (
        "rejected_broker",
        {"lifecycle_state": "rejected"},
        {
            "state": "rejected",
            "void_reason": SECRET,
            "void_reason_group": "broker_rejection",
        },
        None,
        ("rejected_broker", "recorded"),
    ),
    (
        "rejected_broker (throttle)",
        {"lifecycle_state": "rejected"},
        {
            "state": "rejected",
            "void_reason": SECRET,
            "void_reason_group": "provider_throttle",
        },
        None,
        ("rejected_broker", "recorded"),
    ),
    (
        "duplicate_intent",
        {"lifecycle_state": "rejected"},
        {
            "state": "rejected",
            "void_reason": SECRET,
            "void_reason_group": "duplicate_pending_intent",
        },
        None,
        ("duplicate_intent", "recorded"),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "p_kw", "r_kw", "want_p", "want_r"), CASES, ids=[c[0] for c in CASES]
)
async def test_each_reason_comes_from_stored_columns(
    name: str, p_kw: dict, r_kw: dict, want_p: Any, want_r: Any
) -> None:
    got = await rows(p_kw, r_kw)
    assert block(got["proposal"]) == (want_p or (None, None))
    assert block(got["rung"]) == (want_r or (None, None))
    # The raw stored text never reaches the response.
    assert SECRET not in json.dumps(got, ensure_ascii=False)


def test_every_vocabulary_value_has_a_fixture() -> None:
    seen = {c[3][0] for c in CASES if c[3]} | {c[4][0] for c in CASES if c[4]}
    assert seen == set(BLOCK_REASONS)


def test_vocabulary_matches_the_contract_enum() -> None:
    spec = yaml.safe_load(CONTRACT.read_text())["components"]["schemas"]
    assert tuple(spec["BlockReason"]["enum"]) == BLOCK_REASONS
    chain = spec["ChainRow"]["properties"]
    assert chain["block_reason"] == {"$ref": "#/components/schemas/BlockReason"}
    assert chain["block_reason_basis"]["enum"] == ["recorded", "inference"]
    assert "block_reason" not in spec["ChainRow"]["required"]


# --------------------------------------------------------------------------
# no evidence -> no key
# --------------------------------------------------------------------------
UNDERIVABLE: list[tuple[str, dict, dict]] = [
    ("submitted work", {"lifecycle_state": "submitted"}, {"state": "resting"}),
    ("filled", {"lifecycle_state": "filled"}, {"state": "filled"}),
    (
        "proposed with no dispatch evidence",
        {"lifecycle_state": "proposed"},
        {"state": "pending_approval"},
    ),
    (
        "auto veto card is not a wait for approval",
        {
            "lifecycle_state": "proposed",
            "approval_dispatch_state": "sent_current",
            "approval_dispatch_card_kind": "auto_veto",
        },
        {"state": "pending_approval"},
    ),
    (
        "answered card (nonce consumed)",
        {
            "lifecycle_state": "proposed",
            "approval_nonce_used_at": CREATED,
            **HUMAN_CARD,
        },
        {"state": "pending_approval"},
    ),
    (
        "auto approved order with old rejection history",
        {
            "lifecycle_state": "proposed",
            "source_asof": {**REJECTIONS, "auto_approved": {"mode": "expanded"}},
        },
        {"state": "pending_approval"},
    ),
    (
        "unknown window code",
        window("SOMETHING_NEW"),
        {"state": "pending_approval"},
    ),
    (
        "dispatch failure that is not a window verdict",
        {
            "lifecycle_state": "proposed",
            "approval_dispatch_state": "failed",
            "approval_dispatch_failure_code": "telegram_transport_error",
        },
        {"state": "pending_approval"},
    ),
    (
        "window code on a card that was later sent",
        {
            "lifecycle_state": "proposed",
            "approval_dispatch_state": "sent_current",
            "approval_dispatch_failure_code": "DEFER_SESSION_CLOSED/x",
        },
        {"state": "pending_approval"},
    ),
    (
        "day-order expiry of an order that reached the broker",
        {"lifecycle_state": "expired"},
        {"state": "expired", "broker_order_id": "0000123456"},
    ),
    (
        "rejected rung with no classified group",
        {"lifecycle_state": "rejected"},
        {"state": "rejected", "void_reason": SECRET},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "p_kw", "r_kw"), UNDERIVABLE, ids=[c[0] for c in UNDERIVABLE]
)
async def test_without_deciding_evidence_the_keys_are_absent(
    name: str, p_kw: dict, r_kw: dict
) -> None:
    got = await rows(p_kw, r_kw)
    for stage in ("proposal", "rung"):
        assert "block_reason" not in got[stage], (name, stage)
        assert "block_reason_basis" not in got[stage], (name, stage)


def test_precedence_window_verdict_over_demotion_over_waiting() -> None:
    base = {
        "lifecycle_state": "proposed",
        "source_asof": REJECTIONS,
        **HUMAN_CARD,
    }
    assert proposal_block_reason(ns(**base), []) == ("auto_approve_demoted", "recorded")
    blocked = ns(
        **{
            **base,
            "approval_dispatch_state": "failed",
            "approval_dispatch_failure_code": "DEFER_SESSION_CLOSED/x",
        }
    )
    assert proposal_block_reason(blocked, []) == ("deferred_session_closed", "recorded")
    assert rung_block_reason(blocked, ns(state="pending_approval")) == (
        "deferred_session_closed",
        "recorded",
    )


def test_malformed_stored_values_yield_nothing_instead_of_raising() -> None:
    odd = ns(
        lifecycle_state="proposed",
        approval_dispatch_state=7,
        approval_dispatch_failure_code=["DEFER_SESSION_CLOSED"],
        source_asof={"auto_approve_rejections": "oops"},
        approval_dispatch_card_kind=None,
    )
    assert proposal_block_reason(odd, []) is None
    assert rung_block_reason(odd, ns(state=None)) is None
    assert block_fields(None) == {}


# --------------------------------------------------------------------------
# existing rows are unchanged
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_standard_day_has_no_block_reason_anywhere() -> None:
    response = await read_retro_day(kr_day_session(), market="kr", date=DAY)
    assert all("block_reason" not in r for r in response["data"])
