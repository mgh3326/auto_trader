"""retro_timeline plan source: the five closed no-buy reasons (hk 7005).

Design 6939 §6 / ticket N15. One fixture per reason plus ``source_unavailable``;
the derivation itself is a pure function over the stored evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest

from app.services.retro_v1 import read_retro_timeline
from app.services.retro_v1.plan_source import (
    GUARD_REFUSALS,
    PLAN_REASONS,
    PlanEvidence,
    PlanRow,
    derive_plan_reason,
)
from tests.services.retro_v1.support import (
    FakeSession,
    WithParams,
    ns,
    operational_error,
    pick,
    validate,
)

pytestmark = pytest.mark.unit

DAY = date(2026, 1, 5)
HEAD = UUID(int=0x7005)
VERSION = UUID(int=0x7006)
BUNDLE = UUID(int=0x7007)
INTENT = UUID(int=0x7008)
TRIM_INTENT = UUID(int=0x7009)
PICK_AT = datetime(2026, 1, 5, 0, 30, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)
ROW_KEY = '["candidate","kr","005930",null]'


def row(
    *,
    action: str = "new",
    execution_state: str = "executable",
    body: dict[str, Any] | None = None,
) -> PlanRow:
    return PlanRow(
        seq=1,
        published_at=PUBLISHED_AT,
        row_kind="candidate",
        market="kr",
        symbol="005930",
        account_ref=None,
        action=action,
        execution_state=execution_state,
        body=body if body is not None else {"valid_until": VALID_UNTIL.isoformat()},
        key=ROW_KEY,
        change_seq=1,
        ref="plan-0123456789abcdef0123",
    )


# --------------------------------------------------------------------------
# the pure derivation: one case per reason, and the unknown fallback
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        # a read that failed or was cut off decides nothing
        (PlanEvidence(plan_unread=True), "source_unavailable"),
        # absent from the bundle candidate set
        (
            PlanEvidence(bundle_known=True, in_bundle=False),
            "candidate_not_captured",
        ),
        # in the bundle but no plan row: the stored evidence does not decide
        (
            PlanEvidence(bundle_known=True, in_bundle=True),
            "source_unavailable",
        ),
        # executable, expired, no child
        (
            PlanEvidence(row=row(), expired=True),
            "condition_not_reached",
        ),
        # executable, expired, but a child outcome is unknown
        (
            PlanEvidence(row=row(), expired=True, child_unresolved=True),
            "source_unavailable",
        ),
        # 6790 score snapshot has an unavailable input
        (
            PlanEvidence(
                row=row(action="watch", execution_state="plan_only"),
                expired=True,
                input_unknown=True,
            ),
            "data_delayed_or_missing",
        ),
        # 6790 allocation held or passed the row
        (
            PlanEvidence(row=row(), expired=True, allocation_waiting=True),
            "budget_or_guard_block",
        ),
        # an E1 cap/kill/arming/staleness refusal
        (
            PlanEvidence(row=row(), expired=True, guard_refused=True),
            "budget_or_guard_block",
        ),
        # accepted child, no in-effect fill, expired
        (
            PlanEvidence(row=row(), expired=True, child_accepted=True, filled=False),
            "unfilled",
        ),
        # a refused amendment after an accepted, unfilled submit is not the
        # reason: the accepted lineage is the attempt that did not fill
        (
            PlanEvidence(
                row=row(),
                expired=True,
                child_accepted=True,
                filled=False,
                guard_refused=True,
            ),
            "unfilled",
        ),
        # accepted child, ledger unread: never "unfilled" without evidence
        (
            PlanEvidence(
                row=row(), expired=True, child_accepted=True, fills_known=False
            ),
            "source_unavailable",
        ),
        # a fill happened: none of the five describes it, even with a refusal
        (
            PlanEvidence(
                row=row(),
                expired=True,
                child_accepted=True,
                filled=True,
                guard_refused=True,
            ),
            "source_unavailable",
        ),
        # an uncertain send beside an accepted, unfilled child may have filled
        (
            PlanEvidence(
                row=row(),
                expired=True,
                child_accepted=True,
                filled=False,
                child_unresolved=True,
            ),
            "source_unavailable",
        ),
        # accepted but not yet expired
        (
            PlanEvidence(row=row(), expired=False, child_accepted=True, filled=False),
            "source_unavailable",
        ),
        # a refused attempt proves the condition was reached; its reason is
        # outside the five, so it never becomes condition_not_reached
        (
            PlanEvidence(row=row(), expired=True, child_refused_other=True),
            "source_unavailable",
        ),
        # a refused non-guard attempt beside an accepted, unfilled submit does
        # not change that the accepted attempt is the one that did not fill
        (
            PlanEvidence(
                row=row(),
                expired=True,
                child_accepted=True,
                filled=False,
                child_refused_other=True,
            ),
            "unfilled",
        ),
        # an unresolved child outranks a refusal
        (
            PlanEvidence(
                row=row(),
                expired=True,
                child_unresolved=True,
                guard_refused=True,
            ),
            "source_unavailable",
        ),
        # a watch row that never expired is not yet explained
        (
            PlanEvidence(row=row(action="watch", execution_state="plan_only")),
            "source_unavailable",
        ),
        # a sell row is not a no-buy question
        (
            PlanEvidence(
                row=row(action="trim", execution_state="plan_only"),
                expired=True,
            ),
            "source_unavailable",
        ),
        # a plan source read that failed cannot prove child absence
        (
            PlanEvidence(row=row(), expired=True, children_known=False),
            "source_unavailable",
        ),
    ],
)
def test_derive_plan_reason_is_the_closed_design_section_6_table(
    evidence: PlanEvidence, expected: str
) -> None:
    assert derive_plan_reason(evidence) == expected


def test_plan_reasons_are_exactly_the_five_plus_the_unknown_fallback() -> None:
    assert PLAN_REASONS == (
        "candidate_not_captured",
        "condition_not_reached",
        "data_delayed_or_missing",
        "budget_or_guard_block",
        "unfilled",
        "source_unavailable",
    )


def test_guard_refusals_are_the_exact_closed_set() -> None:
    """Deleting a required code or adding an unrelated one must fail here."""
    assert GUARD_REFUSALS == frozenset(
        {
            "quantity_cap_exceeded",
            "notional_cap_exceeded",
            "locked_envelope_cap_exceeded",
            "inventory_constraint_exceeded",
            "sleeve_budget_exceeded",
            "sleeve_loss_limit_reached",
            "max_open_intents_exceeded",
            "submissions_killed",
            "forwarder_unavailable",
            "cooldown_active",
            "plan_version_stale",
            "prior_intent_unsettled",
            "entry_no_fill_window",
        }
    )
    # every code but the design's future forwarder code is a stored Refusal
    from app.services.execution_intent_gateway.decisions import Refusal

    assert GUARD_REFUSALS - {"entry_no_fill_window"} <= {item.value for item in Refusal}


# --------------------------------------------------------------------------
# reader fixtures: one per reason
# --------------------------------------------------------------------------
def plan_row(
    symbol: str = "005930",
    *,
    row_kind: str = "candidate",
    action: str = "new",
    execution_state: str = "executable",
    body: dict[str, Any] | None = None,
    account_ref: str | None = None,
    id_: int = 1,
    version_id: UUID = VERSION,
) -> Any:
    default = {
        "valid_until": VALID_UNTIL.isoformat(),
        "not_executable_reason": None,
        "side": "buy" if action in ("new", "add") else None,
        "venue": "KRX",
    }
    return ns(
        id=id_,
        version_id=version_id,
        row_kind=row_kind,
        market="kr",
        symbol=symbol,
        account_ref=account_ref,
        action=action,
        execution_state=execution_state,
        body=body if body is not None else default,
    )


def version(
    *,
    metadata: dict[str, Any] | None = None,
    bundle_id: UUID | None = None,
    seq: int = 1,
    kind: str = "premarket",
    id_: UUID = VERSION,
) -> Any:
    return ns(
        id=id_,
        seq=seq,
        kind=kind,
        created_at=PUBLISHED_AT,
        metadata=metadata or {},
        input_bundle_id=bundle_id,
    )


def bundle(*symbols: str) -> dict[str, Any]:
    return {
        "rows": [
            {"row_kind": "candidate", "market": "kr", "symbol": symbol}
            for symbol in symbols
        ]
    }


def intent(
    symbol: str = "005930",
    *,
    plan_version: int = 1,
    side: str = "buy",
    row_key: str = ROW_KEY,
    id_: UUID = INTENT,
) -> Any:
    return ns(
        id=id_,
        symbol=symbol,
        side=side,
        account="kis_mock",
        venue="KRX",
        plan_row_key=row_key,
        plan_version=plan_version,
    )


def child(
    *,
    state: str = "accepted",
    refusal_reason: str | None = None,
    broker_order_id: str | None = "0000123456",
    action: str = "submit",
    intent_id: UUID = INTENT,
    created_at: datetime = PICK_AT,
) -> Any:
    return ns(
        intent_id=intent_id,
        action=action,
        state=state,
        refusal_reason=refusal_reason,
        broker_order_id=broker_order_id,
        created_at=created_at,
    )


def fill(
    *,
    order_id: str = "0000123456",
    filled_at: datetime = PICK_AT,
    symbol: str = "005930",
    side: str = "buy",
) -> Any:
    return ns(
        broker="kis",
        account_mode="mock",
        venue="KRX",
        symbol=symbol,
        side=side,
        currency="KRW",
        plan_order_id=order_id,
        plan_filled_at=filled_at,
    )


def plan_session(
    *,
    symbols: tuple[str, ...] = ("005930",),
    rows: list[Any],
    versions: list[Any],
    bundle_body: dict[str, Any] | None = None,
    intents: list[Any] | None = None,
    children: list[Any] | None = None,
    fills: list[Any] | None = None,
    head: Any = HEAD,
) -> FakeSession:
    picks = [pick(index, symbol, PICK_AT) for index, symbol in enumerate(symbols, 1)]
    return FakeSession(
        {
            # the plan's own ledger read carries a label, so it never shares a
            # route with the chain's fill reader
            "plan_order_id": fills or [],
            "FROM review.screener_pick_log": lambda sql: (
                [1] if "count(" in sql.lower() else picks
            ),
            "FROM review.daily_plan_heads": [ns(id=head)] if head is not None else [],
            "FROM review.daily_plan_versions": versions,
            "FROM review.daily_plan_rows": rows,
            "FROM review.daily_plan_input_bundles": (
                [ns(body=bundle_body)] if bundle_body is not None else []
            ),
            "FROM review.execution_intents": intents or [],
            "FROM review.execution_child_requests": children or [],
        }
    )


async def timeline(**kwargs: Any) -> dict[str, Any]:
    response = await read_retro_timeline(plan_session(**kwargs), date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    return response


def only(response: dict[str, Any], symbol: str = "005930") -> dict[str, Any]:
    return next(item for item in response["data"] if item["symbol"] == symbol)


@pytest.mark.asyncio
async def test_candidate_not_captured_is_a_symbol_absent_from_the_bundle() -> None:
    response = await timeline(
        symbols=("000100",),
        rows=[],
        versions=[version(bundle_id=BUNDLE)],
        bundle_body=bundle("005930"),
    )
    item = only(response, "000100")
    assert item["plan_reason"] == "candidate_not_captured"
    assert [e for e in item["events"] if e["source"] == "plan"] == []


@pytest.mark.asyncio
async def test_condition_not_reached_is_an_expired_executable_row_with_no_child() -> (
    None
):
    response = await timeline(
        rows=[plan_row()],
        versions=[version(bundle_id=BUNDLE)],
        bundle_body=bundle("005930"),
    )
    item = only(response)
    assert item["plan_reason"] == "condition_not_reached"
    plan_events = [e for e in item["events"] if e["source"] == "plan"]
    assert [(e["kind"], e["reason"]) for e in plan_events] == [
        ("published_executable", "new"),
        ("expired", None),
    ]
    # bound to the same plan row, and opaque (no head id / row key text)
    assert plan_events[0]["ref"] == plan_events[1]["ref"]
    assert plan_events[0]["ref"].startswith("plan-")
    assert str(HEAD) not in json.dumps(response)
    # the expiry instant is the newest plan evidence
    assert response["data_as_of"] == "2026-01-05T06:30:00Z"


@pytest.mark.asyncio
async def test_data_delayed_or_missing_is_an_unavailable_score_input() -> None:
    allocation = {
        "candidates": [
            {
                "symbol": "005930",
                "inputs": {
                    "rsi_14": {
                        "state": "unknown",
                        "reason": "input_missing",
                        "value": None,
                    }
                },
                "rank": 1,
            }
        ],
        "budget": {},
    }
    response = await timeline(
        rows=[plan_row(action="watch", execution_state="plan_only")],
        versions=[version(metadata={"allocation_comparison": allocation})],
    )
    item = only(response)
    assert item["plan_reason"] == "data_delayed_or_missing"
    # the operator-only comparison is read, never served
    assert "allocation_comparison" not in json.dumps(response)
    assert '"rank"' not in json.dumps(response)


@pytest.mark.asyncio
async def test_budget_or_guard_block_is_an_e1_guard_refusal() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="refused", refusal_reason="quantity_cap_exceeded")],
    )
    assert only(response)["plan_reason"] == "budget_or_guard_block"


@pytest.mark.asyncio
async def test_a_non_guard_refusal_is_not_condition_not_reached() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="refused", refusal_reason="gateway_disabled")],
    )
    assert only(response)["plan_reason"] == "source_unavailable"


@pytest.mark.asyncio
async def test_budget_or_guard_block_is_a_waiting_allocation_too() -> None:
    allocation = {
        "candidates": [],
        "budget": {
            "A": {
                "W": {
                    "allocations": [{"symbol": "005930", "status": "reserved_waiting"}]
                }
            }
        },
    }
    response = await timeline(
        rows=[plan_row(action="watch", execution_state="plan_only")],
        versions=[version(metadata={"allocation_comparison": allocation})],
    )
    assert only(response)["plan_reason"] == "budget_or_guard_block"


@pytest.mark.asyncio
async def test_unfilled_is_an_accepted_child_with_no_in_effect_fill() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="accepted", broker_order_id="0000123456")],
        fills=[],
    )
    item = only(response)
    assert item["plan_reason"] == "unfilled"
    assert any(e["kind"] == "expired" for e in item["events"] if e["source"] == "plan")


@pytest.mark.asyncio
async def test_a_matching_fill_means_the_buy_happened() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="accepted", broker_order_id="0000123456")],
        fills=[fill(order_id="0000123456")],
    )
    assert only(response)["plan_reason"] == "source_unavailable"


@pytest.mark.asyncio
async def test_a_fill_on_another_kst_order_day_does_not_count() -> None:
    """KIS reuses order numbers across days; only the send's day names the order."""
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="accepted", broker_order_id="0000123456")],
        fills=[fill(order_id="0000123456", filled_at=datetime(2026, 1, 6, tzinfo=UTC))],
    )
    assert only(response)["plan_reason"] == "unfilled"


@pytest.mark.asyncio
async def test_a_fill_under_another_symbol_does_not_count() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="accepted", broker_order_id="0000123456")],
        fills=[fill(order_id="0000123456", symbol="000100")],
    )
    assert only(response)["plan_reason"] == "unfilled"


@pytest.mark.asyncio
async def test_an_accepted_amendment_replacement_fill_counts() -> None:
    """kis_mock amend returns a new order id; the replacement is the same buy."""
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[
            child(state="accepted", broker_order_id="0000111111"),
            child(
                state="accepted",
                action="amend",
                broker_order_id="0000222222",
            ),
        ],
        fills=[fill(order_id="0000222222")],
    )
    assert only(response)["plan_reason"] == "source_unavailable"


@pytest.mark.asyncio
async def test_an_uncertain_child_is_never_one_of_the_five() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[child(state="uncertain", broker_order_id="0000123456")],
    )
    assert only(response)["plan_reason"] == "source_unavailable"


@pytest.mark.asyncio
async def test_a_trim_intent_on_the_same_symbol_does_not_decide_the_buy() -> None:
    response = await timeline(
        rows=[plan_row()],
        versions=[version()],
        intents=[
            intent(
                row_key='["holding","kr","005930","acct_0123456789abcdef0123456789abcdef"]',
                id_=TRIM_INTENT,
            )
        ],
        children=[
            child(
                intent_id=TRIM_INTENT,
                state="accepted",
                broker_order_id="0000999999",
            )
        ],
    )
    # the candidate buy row has no child of its own, so it never fired
    assert only(response)["plan_reason"] == "condition_not_reached"


@pytest.mark.asyncio
async def test_a_superseded_intent_does_not_decide_the_new_revision() -> None:
    first = plan_row(
        body={"valid_until": VALID_UNTIL.isoformat(), "max_buy_price": "70000"}
    )
    second = plan_row(
        body={"valid_until": VALID_UNTIL.isoformat(), "max_buy_price": "71000"},
        id_=2,
        version_id=UUID(int=0x7010),
    )
    response = await timeline(
        rows=[first, second],
        versions=[version(), version(seq=2, id_=UUID(int=0x7010))],
        intents=[intent(plan_version=1)],
        children=[child(state="refused", refusal_reason="quantity_cap_exceeded")],
    )
    # seq 2 changed the execution fields, so the seq 1 intent is not this row
    assert only(response)["plan_reason"] == "condition_not_reached"


@pytest.mark.asyncio
async def test_an_unchanged_republication_keeps_the_intent_valid() -> None:
    first = plan_row()
    second = plan_row(id_=2, version_id=UUID(int=0x7010))
    response = await timeline(
        rows=[first, second],
        versions=[
            version(),
            version(seq=2, kind="session", id_=UUID(int=0x7010)),
        ],
        intents=[intent(plan_version=1)],
        children=[child(state="refused", refusal_reason="quantity_cap_exceeded")],
    )
    assert only(response)["plan_reason"] == "budget_or_guard_block"


@pytest.mark.asyncio
async def test_the_reason_uses_the_comparison_of_the_row_version() -> None:
    """A later publication on another row must not rewrite an earlier row's inputs."""
    published = {
        "candidates": [
            {
                "symbol": "005930",
                "inputs": {"rsi_14": {"state": "unknown", "reason": "input_missing"}},
            }
        ],
        "budget": {},
    }
    later = {
        "candidates": [
            {
                "symbol": "005930",
                "inputs": {
                    "rsi_14": {"state": "present", "reason": None, "value": "50"}
                },
            }
        ],
        "budget": {},
    }
    response = await timeline(
        rows=[
            plan_row(action="watch", execution_state="plan_only"),
            plan_row("000100", id_=2, version_id=UUID(int=0x7010)),
        ],
        versions=[
            version(metadata={"allocation_comparison": published}),
            version(
                seq=2,
                kind="session",
                metadata={"allocation_comparison": later},
                id_=UUID(int=0x7010),
            ),
        ],
    )
    assert only(response)["plan_reason"] == "data_delayed_or_missing"


@pytest.mark.asyncio
async def test_a_symbol_filtered_request_filters_the_plan_rows() -> None:
    all_rows = [plan_row("005930"), plan_row("000100", id_=2)]

    def filtered(sql: str, params: dict[str, Any]) -> list[Any]:
        wanted = {
            symbol
            for key, value in params.items()
            if key.startswith("symbol")
            for symbol in value
        }
        return [item for item in all_rows if item.symbol in wanted]

    session = plan_session(symbols=("005930",), rows=[], versions=[version()])
    session.routes["FROM review.daily_plan_rows"] = WithParams(filtered)
    response = await read_retro_timeline(
        session, date=DAY, market="kr", symbol="005930"
    )
    validate(response, "RetroTimelineResponse")
    assert [item["symbol"] for item in response["data"]] == ["005930"]
    assert response["total"] == 1
    # the read really carries the normalized symbol predicate
    rows_sql = [s for s in session.statements if "review.daily_plan_rows" in s]
    assert rows_sql and all("daily_plan_rows.symbol IN" in s for s in rows_sql)


@pytest.mark.asyncio
async def test_every_in_force_row_of_a_symbol_publishes_its_own_event() -> None:
    response = await timeline(
        rows=[
            plan_row("005930"),
            plan_row(
                "005930",
                row_kind="holding",
                action="trim",
                execution_state="plan_only",
                account_ref="acct_0123456789abcdef0123456789abcdef",
                id_=2,
            ),
        ],
        versions=[version()],
    )
    item = only(response)
    plan_events = [e for e in item["events"] if e["source"] == "plan"]
    assert sorted(e["kind"] for e in plan_events) == [
        "expired",
        "expired",
        "published_executable",
        "published_watch",
    ]
    # the candidate buy row still decides the reason
    assert item["plan_reason"] == "condition_not_reached"


@pytest.mark.asyncio
async def test_a_failed_plan_read_is_source_unavailable_with_an_unknown() -> None:
    session = plan_session(
        rows=[plan_row()],
        versions=[version()],
    )
    session.routes["FROM review.daily_plan_heads"] = operational_error()
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    item = only(response)
    assert item["plan_reason"] == "source_unavailable"
    # plan rows add symbols to the population, so an unread plan has no total
    assert response["total"] is None
    assert ("total", "count_unavailable") in {
        (entry["field"], entry["reason"]) for entry in response["unknowns"]
    }
    assert ("source.plan", "upstream_error") in {
        (entry["field"], entry["reason"]) for entry in response["unknowns"]
    }
    assert "raw driver text" not in str(response)


@pytest.mark.asyncio
async def test_a_failed_bundle_read_only_blocks_absence_evidence() -> None:
    session = plan_session(
        rows=[plan_row()],
        versions=[version(bundle_id=BUNDLE)],
        bundle_body=bundle("005930"),
    )
    session.routes["FROM review.daily_plan_input_bundles"] = operational_error()
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    # the row exists, so the missing bundle does not change its reason
    assert only(response)["plan_reason"] == "condition_not_reached"
    assert "source.plan_bundle" in {entry["field"] for entry in response["unknowns"]}


@pytest.mark.asyncio
async def test_a_failed_child_read_makes_child_absence_unproven() -> None:
    session = plan_session(
        rows=[plan_row()],
        versions=[version()],
        intents=[intent()],
        children=[],
    )
    session.routes["FROM review.execution_child_requests"] = operational_error()
    response = await read_retro_timeline(session, date=DAY, market="kr")
    validate(response, "RetroTimelineResponse")
    assert only(response)["plan_reason"] == "source_unavailable"


@pytest.mark.asyncio
async def test_plan_rows_add_their_symbols_to_the_population() -> None:
    response = await timeline(
        symbols=(),
        rows=[plan_row("035720", action="watch", execution_state="plan_only")],
        versions=[version()],
    )
    item = only(response, "035720")
    assert (item["state"], item["reason"]) == ("not_observed", "no_record_in_sources")
    assert [e["kind"] for e in item["events"] if e["source"] == "plan"] == [
        "published_watch",
        "expired",
    ]
