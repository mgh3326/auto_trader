"""Pure reconcile planning: incomplete or disagreeing evidence never closes a row."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.brokers.nhplug.order_evidence import (
    OrderListing,
    assemble_listing,
    classify_listing_page,
)
from app.services.nhplug_mock.reconcile_plan import LedgerRowView, plan_reconcile

pytestmark = pytest.mark.unit

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "nhplug_stage2"
        / "responses.json"
    ).read_text(encoding="utf-8")
)


def fx(name: str) -> dict[str, Any]:
    return copy.deepcopy(FIXTURES[name])


def listing(scope: str, payload: dict[str, Any]) -> OrderListing:
    return assemble_listing(scope, [classify_listing_page(payload)])


def rows_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"rsp_cd": "00000", "Output_1": list(rows)}


def base_row(**overrides: Any) -> dict[str, Any]:
    row = fx("listing_open_one")["Output_1"][0]
    row.update(overrides)
    return row


EMPTY = rows_payload()


def view(**overrides: Any) -> LedgerRowView:
    values: dict[str, Any] = {
        "id": 1,
        "operation_kind": "place",
        "status": "accepted",
        "symbol": "005930",
        "side": "buy",
        "quantity": 1,
        "price": Decimal(50000),
        "broker_order_id": "1000123",
        "original_order_id": None,
    }
    values.update(overrides)
    return LedgerRowView(**values)


def plan_one(
    row: LedgerRowView,
    all_payload: dict[str, Any],
    open_payload: dict[str, Any],
    claimed: tuple[str | None, ...] | None = None,
    filled_payload: dict[str, Any] | None = None,
):
    if filled_payload is None:
        rows = all_payload.get("Output_1") or []
        filled_payload = rows_payload(
            *[r for r in rows if isinstance(r, dict) and r.get("tot_cns_qty")]
        )
    planned = plan_reconcile(
        [row],
        all_listing=listing("all", all_payload),
        open_listing=listing("open", open_payload),
        filled_listing=listing("filled", filled_payload),
        all_claimed_order_ids=claimed
        if claimed is not None
        else (row.broker_order_id,),
    )
    assert len(planned) <= 1
    return planned[0].update if planned else None


def test_open_order_confirmed_by_both_sources() -> None:
    update = plan_one(view(), rows_payload(base_row()), rows_payload(base_row()))
    assert (update.status, update.reconcile_state) == ("open", "verified")
    assert update.open_qty == 1 and update.filled_qty == 0


def test_filled_needs_all_scope_fill_and_absence_from_complete_open_scope() -> None:
    filled = base_row(tot_cns_qty=1, ny_cns_qty=0, cns_avg_uit_pr="50000.000")
    update = plan_one(view(), rows_payload(filled), EMPTY)
    assert (update.status, update.reconcile_state) == ("filled", "verified")
    assert update.filled_qty == 1 and update.avg_fill_price == Decimal(50000)
    assert update.evidence["order_no"] == "1000123"


@pytest.mark.parametrize(
    "broken_open",
    (
        "listing_processing_error_00007",
        "listing_gateway_error",
        "listing_rows_not_list",
        "listing_malformed_row",
    ),
)
def test_terminal_fill_is_not_booked_when_open_scope_is_error_shaped(
    broken_open: str,
) -> None:
    filled = base_row(tot_cns_qty=1, ny_cns_qty=0)
    update = plan_one(view(), rows_payload(filled), fx(broken_open))
    assert update.status is None
    assert update.reconcile_state == "unknown"
    assert update.filled_qty is None


def test_terminal_state_with_order_still_in_open_scope_is_disagreement() -> None:
    filled = base_row(tot_cns_qty=1, ny_cns_qty=0)
    update = plan_one(view(), rows_payload(filled), rows_payload(base_row()))
    assert update.status is None
    assert update.reconcile_state == "source_disagreement"


def test_open_in_all_scope_but_missing_from_open_scope_is_disagreement() -> None:
    """kt00009 lesson: the open endpoint's [] never closes an order."""

    update = plan_one(
        view(), rows_payload(base_row()), fx("listing_empty_array_success")
    )
    assert update.status is None
    assert update.reconcile_state == "source_disagreement"


@pytest.mark.parametrize(
    "all_payload",
    (
        "listing_block_absent_success",
        "listing_empty_array_success",
        "listing_no_records_13578",
    ),
)
def test_order_missing_from_an_empty_all_scope_is_unknown_not_closed(
    all_payload: str,
) -> None:
    update = plan_one(view(), fx(all_payload), fx("listing_empty_array_success"))
    assert update.status is None
    assert update.reconcile_state == "unknown"
    assert update.note["reason"] == "order_missing_from_all_orders_listing"


@pytest.mark.parametrize(
    "broken_all",
    (
        "listing_processing_error_00007",
        "listing_gateway_error",
        "listing_malformed_row",
    ),
)
def test_incomplete_all_scope_marks_every_live_row_unknown(broken_all: str) -> None:
    update = plan_one(view(), fx(broken_all), EMPTY)
    assert update.status is None
    assert update.reconcile_state == "unknown"
    assert update.note["reason"].startswith("all_scope_incomplete")


def test_symbol_mismatch_is_anomaly_for_manual_review() -> None:
    update = plan_one(
        view(),
        rows_payload(base_row(iem_cd="000660")),
        rows_payload(base_row(iem_cd="000660")),
    )
    assert update.status == "anomaly"
    assert update.requires_manual_review is True


def test_inconsistent_quantities_are_unknown() -> None:
    update = plan_one(view(), rows_payload(base_row(ny_cns_qty=0)), EMPTY)
    assert update.reconcile_state == "unknown"
    assert update.status is None


def test_partially_filled_regressing_to_open_is_unknown() -> None:
    update = plan_one(
        view(status="partially_filled"),
        rows_payload(base_row()),
        rows_payload(base_row()),
    )
    assert update.reconcile_state == "unknown"


def test_uncertain_submission_binds_only_a_unique_unclaimed_match() -> None:
    uncertain = view(status="acceptance_uncertain", broker_order_id=None)
    update = plan_one(uncertain, rows_payload(base_row()), rows_payload(base_row()), ())
    assert update.status == "accepted"
    assert update.broker_order_id == "1000123"
    assert update.requires_manual_review is True

    claimed = plan_one(
        uncertain, rows_payload(base_row()), rows_payload(base_row()), ("1000123",)
    )
    assert claimed.reconcile_state == "unknown"
    assert claimed.note["reason"] == "no_matching_broker_order_found"

    ambiguous = plan_one(
        uncertain,
        rows_payload(base_row(), base_row(itg_orr_no=1000124)),
        EMPTY,
        (),
    )
    assert ambiguous.note["reason"] == "ambiguous_matching_broker_orders"


def test_uncertain_submission_with_no_match_stays_unknown_not_rejected() -> None:
    update = plan_one(
        view(status="acceptance_uncertain", broker_order_id=None), EMPTY, EMPTY, ()
    )
    assert update.status is None
    assert update.reconcile_state == "unknown"


def test_rejected_ack_whose_order_appears_is_anomaly() -> None:
    rejected = view(status="rejected", broker_order_id=None)
    update = plan_one(rejected, rows_payload(base_row()), rows_payload(base_row()), ())
    assert update.status == "anomaly"
    assert plan_one(rejected, EMPTY, EMPTY, ()) is None


def test_modify_and_cancel_chain_from_recorded_listing() -> None:
    after = fx("listing_all_after_cancel")
    original = view(
        id=1, status="accepted", broker_order_id="1000123", ack_order_id="1000123"
    )
    modify = view(
        id=2,
        operation_kind="modify",
        status="accepted",
        broker_order_id="1000130",
        ack_order_id="1000130",
        original_order_id="1000123",
        price=Decimal(49500),
    )
    cancel = view(
        id=3,
        operation_kind="cancel",
        status="accepted",
        broker_order_id="1000140",
        ack_order_id="1000140",
        original_order_id="1000130",
        quantity=None,
        price=None,
    )
    planned = {
        item.row_id: item.update
        for item in plan_reconcile(
            [original, modify, cancel],
            all_listing=listing("all", after),
            open_listing=listing("open", fx("listing_no_records_13578")),
            all_claimed_order_ids=("1000123", "1000130", "1000140"),
        )
    }
    assert planned[1].status == "modified"
    assert planned[2].status == "cancelled"
    assert planned[3].status == "confirmed"
    assert planned[3].cancelled_qty == 1
    assert all(update.reconcile_state == "verified" for update in planned.values())


def test_cancel_is_not_confirmed_while_original_is_open_or_open_scope_broken() -> None:
    cancel = view(
        operation_kind="cancel",
        broker_order_id="1000140",
        original_order_id="1000123",
        quantity=None,
        price=None,
    )
    still_open = plan_one(cancel, rows_payload(base_row()), rows_payload(base_row()))
    assert still_open.status is None and still_open.reconcile_state == "pending"
    broken = plan_one(
        cancel,
        rows_payload(base_row(ny_cns_qty=0, can_qty=1)),
        fx("listing_gateway_error"),
    )
    assert broken.status is None and broken.reconcile_state == "unknown"


def test_terminal_ledger_rows_are_not_replanned() -> None:
    for status in (
        "filled",
        "cancelled",
        "modified",
        "confirmed",
        "anomaly",
        "not_submitted",
    ):
        assert plan_one(view(status=status), rows_payload(base_row()), EMPTY) is None


# --- tester round 1 regressions ---------------------------------------------


def test_fill_requires_confirmation_from_the_filled_scope() -> None:
    filled = base_row(tot_cns_qty=1, ny_cns_qty=0, cns_avg_uit_pr="50000")
    for filled_payload, reason in (
        (EMPTY, "fill_not_confirmed_by_filled_scope"),
        (
            rows_payload(base_row(tot_cns_qty=2, orr_qty=2)),
            "fill_not_confirmed_by_filled_scope",
        ),
        (fx("listing_gateway_error"), "filled_scope_incomplete:gateway_error_envelope"),
    ):
        update = plan_one(
            view(), rows_payload(filled), EMPTY, filled_payload=filled_payload
        )
        assert update.status is None and update.reconcile_state == "unknown", (
            filled_payload
        )
        assert update.note["reason"] == reason
        assert update.filled_qty is None


@pytest.mark.parametrize(
    "open_page",
    ({"rsp_cd": "00000"}, {"rsp_cd": "13578"}, {"rsp_cd": "00000", "Output_1": []}),
)
def test_tester_repro_unfinished_open_page_cannot_book_a_fill(
    open_page: dict[str, Any],
) -> None:
    """R1 finding 4 repro: cts_flag=Y without a key on the open scope."""

    filled = base_row(tot_cns_qty=1, ny_cns_qty=0, cns_avg_uit_pr="50000")
    open_listing = assemble_listing(
        "open",
        [classify_listing_page(open_page, header_continuation_flag="Y")],
    )
    planned = plan_reconcile(
        [view()],
        all_listing=listing("all", rows_payload(filled)),
        open_listing=open_listing,
        filled_listing=listing("filled", rows_payload(filled)),
        all_claimed_order_ids=("1000123",),
    )
    assert planned[0].update.status is None
    assert planned[0].update.reconcile_state == "unknown"


def test_tester_repro_contradictory_quantities_are_not_verified() -> None:
    """R1 finding 5 repro: orr_qty=1, tot_cns_qty=1, ny_cns_qty=1."""

    row = base_row(tot_cns_qty=1, ny_cns_qty=1)
    update = plan_one(view(), rows_payload(row), rows_payload(row))
    assert update.reconcile_state == "unknown"
    assert update.status is None and update.filled_qty is None


def test_attribute_binding_carries_no_evidence_fields() -> None:
    uncertain = view(status="acceptance_uncertain", broker_order_id=None)
    update = plan_one(uncertain, rows_payload(base_row()), rows_payload(base_row()), ())
    assert update.evidence is None and update.filled_qty is None
    assert update.note["evidence"]["order_no"] == "1000123"


# --- tester round 2: terminal states need two positive sources -------------


@pytest.mark.parametrize(
    "open_page",
    ({"rsp_cd": "00000"}, {"rsp_cd": "13578"}, {"rsp_cd": "00000", "Output_1": []}),
    ids=("block_absent", "no_records", "empty_list"),
)
def test_cancel_from_listing_alone_is_not_terminal(open_page: dict[str, Any]) -> None:
    """R2 finding 1 repro: can_qty=1/open=0 plus an empty open scope."""

    cancelled = base_row(ny_cns_qty=0, can_qty=1)
    update = plan_one(view(ack_order_id="1000123"), rows_payload(cancelled), open_page)
    assert update.status is None
    assert update.reconcile_state == "unknown"
    assert update.note["reason"] == "cancel_not_corroborated_by_own_acknowledged_cancel"


def test_cancel_with_own_acknowledged_cancel_is_terminal() -> None:
    cancelled = base_row(ny_cns_qty=0, can_qty=1)
    order = view(id=1, ack_order_id="1000123")
    own_cancel = view(
        id=2,
        operation_kind="cancel",
        broker_order_id="1000140",
        ack_order_id="1000140",
        original_order_id="1000123",
        quantity=None,
        price=None,
    )
    planned = {
        item.row_id: item.update
        for item in plan_reconcile(
            [order, own_cancel],
            all_listing=listing("all", rows_payload(cancelled)),
            open_listing=listing("open", {"rsp_cd": "13578"}),
            filled_listing=listing("filled", {"rsp_cd": "13578"}),
            all_claimed_order_ids=("1000123", "1000140"),
        )
    }
    assert planned[1].status == "cancelled"
    assert planned[2].status == "confirmed"


def test_cancel_row_bound_only_from_listing_is_held_for_review() -> None:
    cancel = view(
        operation_kind="cancel",
        status="acceptance_uncertain",
        broker_order_id=None,
        original_order_id="1000123",
        quantity=None,
        price=None,
    )
    original = base_row(ny_cns_qty=0, can_qty=1)
    cancel_row = base_row(
        itg_orr_no=1000140,
        org_itg_orr_no=1000123,
        cor_can_dit_cd_nm="취소",
        ny_cns_qty=0,
        orr_qty=1,
        can_qty=0,
        tot_cns_qty=0,
    )
    update = plan_one(cancel, rows_payload(original, cancel_row), EMPTY, ("1000123",))
    assert update.status is None
    assert update.broker_order_id == "1000140"
    assert update.requires_manual_review is True


def test_modify_needs_own_acknowledged_modify_and_successor_row() -> None:
    original = base_row(ny_cns_qty=0, cor_qty="1")
    update = plan_one(view(), rows_payload(original), EMPTY)
    assert update.status is None
    assert update.note["reason"] == "modify_not_corroborated_by_own_acknowledged_modify"


def test_listing_rejection_goes_to_manual_review_not_terminal() -> None:
    rejected = base_row(ny_cns_qty=0, orr_rjt_rsn_cd_nm="잔고부족")
    update = plan_one(view(), rows_payload(rejected), EMPTY)
    assert update.status is None and update.reconcile_state == "unknown"
    assert update.requires_manual_review is True
