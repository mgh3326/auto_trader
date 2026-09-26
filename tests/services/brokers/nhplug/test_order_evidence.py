"""Pure evidence rules for NHPLUG Stage 2: empty is never "no open orders"."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.brokers.nhplug.order_evidence import (
    EMPTY_IS_NOT_EVIDENCE,
    OrderListing,
    assemble_listing,
    classify_listing_page,
    derive_order_status,
    determine_open_orders,
    parse_order_row,
    redact_message,
)

pytestmark = pytest.mark.unit

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "nhplug_stage2"
        / "responses.json"
    ).read_text(encoding="utf-8")
)


def fx(name: str) -> dict[str, Any]:
    return copy.deepcopy(FIXTURES[name])


_OPEN_ROW_FOR_KEYS: dict[str, Any] = {
    "itg_orr_no": 1000123,
    "iem_cd": "005930",
    "orr_qty": 1,
    "tot_cns_qty": 0,
    "ny_cns_qty": 1,
}


def listing(scope: str, *names: str) -> OrderListing:
    return assemble_listing(scope, [classify_listing_page(fx(name)) for name in names])


def test_fixture_provenance_is_declared_synthetic() -> None:
    assert "no live NH response was captured" in FIXTURES["_provenance"]


@pytest.mark.parametrize(
    "name",
    (
        "listing_block_absent_success",
        "listing_empty_array_success",
        "listing_no_records_13578",
    ),
)
def test_well_formed_empty_pages_are_usable_but_carry_no_rows(name: str) -> None:
    page = classify_listing_page(fx(name))
    assert page.usable is True
    assert page.rows == ()


@pytest.mark.parametrize(
    ("name", "reason"),
    (
        ("listing_processing_error_00007", "unrecognized_response_code"),
        ("listing_gateway_error", "gateway_error_envelope"),
        ("listing_rows_not_list", "row_block_not_a_list"),
        ("listing_malformed_row", "malformed_order_row"),
    ),
)
def test_error_shaped_pages_are_unusable_not_empty(name: str, reason: str) -> None:
    page = classify_listing_page(fx(name))
    assert page.usable is False
    assert page.reason == reason
    assert page.rows == ()


def test_missing_response_code_and_non_object_are_unusable() -> None:
    assert classify_listing_page({"Output_1": []}).reason == "missing_response_code"
    assert classify_listing_page([]).reason == "response_not_object"
    assert classify_listing_page(None).usable is False


def test_no_rows_code_that_carries_rows_is_contradictory() -> None:
    payload = fx("listing_open_one")
    payload["rsp_cd"] = "13578"
    assert classify_listing_page(payload).reason == "no_rows_code_with_rows"


def test_customer_name_is_never_copied_into_evidence() -> None:
    page = classify_listing_page(fx("listing_open_one"))
    rendered = json.dumps([row.evidence() for row in page.rows], ensure_ascii=False)
    assert "CUSTOMER_NAME_MUST_NOT_LEAK" not in rendered
    assert page.rows[0].side == "buy"
    assert page.rows[0].order_price == Decimal(50000)


def test_continuation_by_code_header_and_flag() -> None:
    first = classify_listing_page(
        fx("listing_page1_continue"), header_continuation_key="K1"
    )
    assert first.has_next and first.continuation_key == "K1"
    keyed_last = classify_listing_page(
        fx("listing_page2_last"), header_continuation_key="K2"
    )
    assert keyed_last.has_next  # any key is followed
    flagged = classify_listing_page(
        fx("listing_page2_last"),
        header_continuation_key="K2",
        header_continuation_flag="Y",
    )
    assert flagged.has_next
    # Tester R2: cts_flag=N contradicted by a continue code or a key is not
    # final; the key is followed (or the listing is incomplete).
    contradicted = classify_listing_page(
        fx("listing_page1_continue"),
        header_continuation_key="K1",
        header_continuation_flag="N",
    )
    assert contradicted.has_next and contradicted.continuation_key == "K1"
    final = classify_listing_page(
        fx("listing_page2_last"), header_continuation_flag="N"
    )
    assert not final.has_next
    body = fx("listing_page2_last")
    body["Output_1"][0]["ctsz20"] = "BODYKEY"
    assert classify_listing_page(body).continuation_key == "BODYKEY"


def test_complete_listing_requires_every_page_and_no_pending_continuation() -> None:
    first = classify_listing_page(
        fx("listing_page1_continue"), header_continuation_key="K1"
    )
    last = classify_listing_page(fx("listing_page2_last"))
    complete = assemble_listing("all", [first, last])
    assert complete.complete and {r.order_no for r in complete.rows} == {
        1000123,
        1000124,
    }
    truncated = assemble_listing("all", [first])
    assert not truncated.complete and truncated.reason == "pagination_truncated"
    looped = assemble_listing("all", [first, first])
    assert not looped.complete and looped.reason == "continuation_key_repeated"
    broken = assemble_listing(
        "all", [first, classify_listing_page(fx("listing_gateway_error"))]
    )
    assert not broken.complete and broken.reason == "gateway_error_envelope"
    assert assemble_listing("all", []).reason == "no_pages"


def test_conflicting_duplicate_rows_make_listing_incomplete() -> None:
    a = classify_listing_page(fx("listing_open_one"), header_continuation_key="K")
    changed = fx("listing_open_one")
    changed["Output_1"][0]["ny_cns_qty"] = 0
    changed["Output_1"][0]["can_qty"] = 1
    b = classify_listing_page(changed)
    result = assemble_listing("all", [a, b])
    assert result.reason == "conflicting_duplicate_order_rows"


# --- The kt00009 lesson: "empty array != no open orders" -------------------


@pytest.mark.parametrize(
    "empty",
    (
        "listing_block_absent_success",
        "listing_empty_array_success",
        "listing_no_records_13578",
    ),
)
@pytest.mark.parametrize(
    "all_payload",
    ("listing_all_filled", "listing_all_after_cancel", "listing_empty_array_success"),
)
def test_empty_open_scope_is_never_no_open_orders(empty: str, all_payload: str) -> None:
    """Tester R1/R2: no empty open-scope shape yields a "none" answer.

    Even with complete, agreeing sources and today's closed orders listed in
    the all-orders scope, the answer is unknown (absence is not evidence).
    """

    result = determine_open_orders(
        all_listing=listing("all", all_payload),
        open_listing=listing("open", empty),
    )
    assert result.state == "unknown"
    assert result.open_rows == ()
    assert result.reasons[-1] == EMPTY_IS_NOT_EVIDENCE


def test_open_states_are_only_present_or_unknown() -> None:
    import typing

    from app.services.brokers.nhplug import order_evidence

    assert set(typing.get_args(order_evidence.OpenOrdersState)) == {
        "present",
        "unknown",
    }


@pytest.mark.parametrize(
    ("payload", "headers"),
    (
        ({"rsp_cd": "00000"}, {"header_continuation_flag": "Y"}),
        ({"rsp_cd": "00000", "Output_1": []}, {"header_continuation_flag": "Y"}),
        ({"rsp_cd": "00165"}, {}),
        ({"rsp_cd": "00218", "Output_1": []}, {}),
    ),
    ids=("flag_y_no_key", "flag_y_empty_rows", "continue_code_no_key", "continue_218"),
)
def test_continuation_without_a_followable_key_is_incomplete(
    payload: dict[str, Any], headers: dict[str, str]
) -> None:
    """Tester R1 finding 4: an announced next page with no key is not final."""

    page = classify_listing_page(payload, **headers)
    assert page.usable and page.has_next and page.continuation_key is None
    assembled = assemble_listing("open", [page])
    assert assembled.complete is False
    assert assembled.reason == "pagination_truncated"


def test_empty_open_scope_is_overruled_by_the_all_orders_scope() -> None:
    """kt00009 regression: the open-only endpoint returns [] while an order rests."""

    result = determine_open_orders(
        all_listing=listing("all", "listing_all_one_open"),
        open_listing=listing("open", "listing_empty_array_success"),
    )
    assert result.state == "present"
    assert [row.order_no for row in result.open_rows] == [1000123]
    assert "open_order_sources_disagree" in result.reasons


@pytest.mark.parametrize(
    "broken",
    (
        "listing_processing_error_00007",
        "listing_gateway_error",
        "listing_rows_not_list",
        "listing_malformed_row",
    ),
)
def test_error_shaped_open_scope_with_empty_all_scope_is_unknown(broken: str) -> None:
    result = determine_open_orders(
        all_listing=listing("all", "listing_empty_array_success"),
        open_listing=listing("open", broken),
    )
    assert result.state == "unknown"
    assert any(reason.startswith("open_scope_incomplete") for reason in result.reasons)


def test_error_shaped_all_scope_with_empty_open_scope_is_unknown() -> None:
    result = determine_open_orders(
        all_listing=listing("all", "listing_gateway_error"),
        open_listing=listing("open", "listing_empty_array_success"),
    )
    assert result.state == "unknown"


def test_truncated_pagination_is_unknown_even_when_rows_are_empty() -> None:
    first = classify_listing_page(
        fx("listing_no_records_13578"),
        header_continuation_key="K",
        header_continuation_flag="Y",
    )
    result = determine_open_orders(
        all_listing=assemble_listing("all", [first]),
        open_listing=listing("open", "listing_no_records_13578"),
    )
    assert result.state == "unknown"


def test_ledger_live_order_missing_from_both_empty_listings_is_unknown() -> None:
    result = determine_open_orders(
        all_listing=listing("all", "listing_empty_array_success"),
        open_listing=listing("open", "listing_empty_array_success"),
        ledger_live_order_nos=[1000999],
    )
    assert result.state == "unknown"
    assert "ledger_live_order_missing_from_listing" in result.reasons


def test_unbound_uncertain_ledger_order_is_reported_as_a_reason() -> None:
    result = determine_open_orders(
        all_listing=listing("all", "listing_empty_array_success"),
        open_listing=listing("open", "listing_empty_array_success"),
        ledger_has_unbound_uncertain=True,
    )
    assert result.state == "unknown"


def test_redact_message_bounds_length() -> None:
    assert redact_message("x" * 500) == "x" * 160
    assert redact_message("") is None


# --- status derivation -----------------------------------------------------


def _row(**overrides: Any) -> Any:
    base = fx("listing_open_one")["Output_1"][0]
    base.update(overrides)
    parsed = parse_order_row(base)
    assert parsed is not None
    return parsed


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({}, "open"),
        ({"orr_qty": 2, "tot_cns_qty": 1, "ny_cns_qty": 1}, "partially_filled"),
        ({"tot_cns_qty": 1, "ny_cns_qty": 0}, "filled"),
        ({"ny_cns_qty": 0, "can_qty": 1}, "cancelled"),
        ({"ny_cns_qty": 0, "cor_qty": "1"}, "modified"),
        ({"ny_cns_qty": 0, "orr_rjt_rsn_cd_nm": "잔고부족"}, "rejected"),
        ({"ny_cns_qty": 0}, "unknown"),
        ({"tot_cns_qty": 5}, "unknown"),
        # Tester R1 finding 5: filled + open exceeds the order quantity.
        ({"tot_cns_qty": 1, "ny_cns_qty": 1}, "unknown"),
        ({"orr_qty": 3, "tot_cns_qty": 1, "ny_cns_qty": 1}, "unknown"),
        (
            {"orr_qty": 3, "tot_cns_qty": 1, "ny_cns_qty": 1, "can_qty": 1},
            "partially_filled",
        ),
        ({"ny_cns_qty": 0, "can_qty": 1, "cor_qty": "1"}, "unknown"),
    ),
)
def test_derive_order_status(overrides: dict[str, Any], expected: str) -> None:
    assert derive_order_status(_row(**overrides)) == expected


@pytest.mark.parametrize(
    "field_value",
    ({"ny_cns_qty": True}, {"orr_qty": -1}, {"tot_cns_qty": 1.5}, {"iem_cd": ""}),
)
def test_row_parsing_rejects_bools_negatives_floats_and_blanks(
    field_value: dict[str, Any],
) -> None:
    base = fx("listing_open_one")["Output_1"][0]
    base.update(field_value)
    assert parse_order_row(base) is None


def test_open_scope_row_without_open_quantity_is_not_presence() -> None:
    """CodeRabbit: contradictory open-scope rows drive unknown, not present."""

    zero = fx("listing_open_one")
    zero["Output_1"][0]["ny_cns_qty"] = 0
    zero["Output_1"][0]["can_qty"] = 1
    result = determine_open_orders(
        all_listing=listing("all", "listing_all_filled"),
        open_listing=assemble_listing("open", [classify_listing_page(zero)]),
    )
    assert result.state == "unknown"
    assert result.open_rows == ()
    assert "open_scope_row_without_open_quantity" in result.reasons


@pytest.mark.parametrize(
    ("payload", "headers", "expected_key"),
    (
        ({"rsp_cd": "00165", "Output_1": []}, {"header_continuation_flag": "N"}, None),
        (
            {"rsp_cd": "00000", "Output_1": [{**_OPEN_ROW_FOR_KEYS, "ctsz20": "BODY"}]},
            {"header_continuation_flag": "N"},
            "BODY",
        ),
        (
            {"rsp_cd": "00000", "Output_1": []},
            {"header_continuation_key": "HDR"},
            "HDR",
        ),
    ),
    ids=("continue_code_with_flag_n", "body_key_with_flag_n", "header_key_no_flag"),
)
def test_tester_r2_contradictory_continuation_is_never_final(
    payload: dict[str, Any], headers: dict[str, str], expected_key: str | None
) -> None:
    page = classify_listing_page(payload, **headers)
    assert page.usable and page.has_next
    assert page.continuation_key == expected_key
    # A single such page (next page not fetched) is never a complete listing.
    assert assemble_listing("open", [page]).complete is False
