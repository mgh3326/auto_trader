"""#1268 — pure tests for the D2 root reconcile eligibility and evidence rules."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo import d2_root_reconcile as r
from tests.services.brokers.binance.spot_demo._d2_root_fixtures import (
    BTC,
    ETH,
    EVIDENCE_REFUSALS,
    ROW_REFUSALS,
    USDC,
    FakeSpotDemoReader,
    fake_instrument,
    fake_row,
    filled_body,
    refusal_evidence,
    refusal_row,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ ids


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["442"], (442,)),
        (["442,443,444"], (442, 443, 444)),
        (["442", "443", "444"], (442, 443, 444)),
    ],
)
def test_parse_ids_accepts_only_exact_decimal_ids(values, expected) -> None:
    assert r.parse_ids(values) == expected


@pytest.mark.parametrize(
    "values",
    [
        [""],
        ["442-444"],
        ["442..444"],
        ["44*"],
        ["%"],
        ["442 "],
        [" 442"],
        ["+442"],
        ["-1"],
        ["0"],
        ["0442"],
        ["４４２"],
        ["0x1"],
        ["442,442"],
        ["442", "442"],
        ["442,"],
        ["442,443,444,445"],
        [str(2**63)],
        [],
    ],
)
def test_parse_ids_refuses_anything_but_an_exact_short_list(values) -> None:
    with pytest.raises(r.D2RootReconcileInputError):
        r.parse_ids(values)


def test_max_ids_is_the_bound_order_count() -> None:
    assert r.MAX_IDS == 3


@pytest.mark.parametrize("value", [None, "", "   ", "a\x00b", "a​b", "x" * 501])
def test_reason_must_be_bounded_single_line_text(value) -> None:
    with pytest.raises(r.D2RootReconcileInputError):
        r.validate_text("reason", value, max_chars=r.MAX_REASON_CHARS)


# ------------------------------------------------------------- client guard


def test_spot_demo_reader_with_the_sealed_credential_is_accepted() -> None:
    r.assert_spot_demo_reader(FakeSpotDemoReader())


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.binance.com",
        "https://demo-fapi.binance.com",
        "https://testnet.binance.vision",
        "https://fapi.binance.com",
        "https://evil.example",
        "",
    ],
)
def test_non_spot_demo_host_is_refused_before_any_read(base_url: str) -> None:
    client = FakeSpotDemoReader({}, base_url=base_url)
    with pytest.raises(r.D2RootReconcileInputError):
        asyncio.run(r.preview_reconcile(None, client, (442,)))  # type: ignore[arg-type]
    assert client.calls == []


def test_other_credential_is_refused_before_any_read() -> None:
    client = FakeSpotDemoReader({}, credential_fingerprint="sha256:" + "1" * 64)
    with pytest.raises(r.D2RootReconcileInputError):
        asyncio.run(
            r.commit_reconcile(
                None,  # type: ignore[arg-type]
                client,
                (442,),
                reason="hk 1268",
                actor="desk",
            )
        )
    assert client.calls == []


# ------------------------------------------------------------- row verdicts


@pytest.mark.parametrize("order", [BTC, ETH, USDC])
def test_each_filled_d2_root_is_row_eligible(order) -> None:
    verdict = r.evaluate_row(1, fake_row(order), fake_instrument(order.symbol))
    assert verdict.verdict == "d2_filled_root"
    assert verdict.row_eligible
    assert not verdict.eligible  # no broker evidence yet


@pytest.mark.parametrize("verdict", sorted(ROW_REFUSALS))
def test_each_row_refusal_is_isolated(verdict: str) -> None:
    row, instrument = refusal_row(verdict)
    result = r.evaluate_row(7, row, instrument)
    assert result.verdict == verdict
    assert not result.row_eligible
    assert not result.eligible


def test_reconciled_root_without_this_tools_audit_is_refused_not_noop() -> None:
    result = r.evaluate_row(
        7, fake_row(lifecycle_state="reconciled"), fake_instrument()
    )
    assert result.verdict == "not_filled"
    assert not result.already_reconciled


@pytest.mark.parametrize("state", ["anomaly", "closed", "cancelled", "planned"])
def test_every_other_state_is_refused(state: str) -> None:
    assert (
        r.evaluate_row(7, fake_row(lifecycle_state=state), fake_instrument()).verdict
        == "not_filled"
    )


def test_trailing_zero_decimals_are_the_same_bound_order() -> None:
    row = fake_row(qty=Decimal("0.000150000000"), price=Decimal("75421.270000000000"))
    assert r.evaluate_row(1, row, fake_instrument()).verdict == "d2_filled_root"


def test_missing_instrument_is_refused() -> None:
    assert r.evaluate_row(1, fake_row(), None).verdict == "instrument_mismatch"


# ------------------------------------------------------------- evidence


@pytest.mark.parametrize("order", [BTC, ETH, USDC])
def test_fully_filled_matching_order_is_evidence(order) -> None:
    result = r.evaluate_evidence(fake_row(order), filled_body(order))
    assert result.verdict == "broker_filled_match"
    assert result.matches


def test_string_order_id_and_recorded_fill_actual_match() -> None:
    row = fake_row(
        extra_metadata={**fake_row().extra_metadata, "filled_qty": "0.00015"}
    )
    assert r.evaluate_evidence(row, filled_body(broker_order_id="9000001")).matches


@pytest.mark.parametrize("verdict", sorted(EVIDENCE_REFUSALS))
def test_each_evidence_refusal_is_isolated(verdict: str) -> None:
    row, body = refusal_evidence(verdict)
    result = r.evaluate_evidence(row, body)
    assert result.verdict == verdict
    assert not result.matches


@pytest.mark.parametrize(
    "changes",
    [
        {"clientOrderId": None},
        {"clientOrderId": "None"},
        {"orderId": None},
        {"orderId": "None"},
        {"orderId": 0},
        {"orderId": True},
        {"status": "NEW"},
        {"status": "CANCELED"},
        {"status": "EXPIRED"},
        {"price": None},
        {"price": "NaN"},
        {"executedQty": None},
        {"executedQty": "0"},
        {"origQty": "garbage"},
        {"timeInForce": None},
    ],
)
def test_absent_or_spelled_absent_evidence_never_matches(changes) -> None:
    body = filled_body()
    body.update(changes)
    assert not r.evaluate_evidence(fake_row(), body).matches


def test_read_failure_detail_carries_the_class_only() -> None:
    result = r.evaluate_evidence(fake_row(), RuntimeError("https://x?signature=abc"))
    assert result.detail == {"error_class": "RuntimeError"}


# ------------------------------------------------------------- batch decision


def _ok(ledger_id: int) -> r.RowVerdict:
    return r.RowVerdict(
        ledger_id,
        "d2_filled_root",
        {"client_order_id": "c"},
        r.EvidenceVerdict("broker_filled_match"),
    )


def _done(ledger_id: int) -> r.RowVerdict:
    return r.RowVerdict(ledger_id, "already_reconciled")


def test_decide_all_eligible() -> None:
    assert r.decide((1, 2, 3), (_ok(1), _ok(2), _ok(3))) == "eligible"


def test_decide_one_bad_row_refuses_the_batch() -> None:
    bad = r.RowVerdict(3, "not_d2_writer")
    assert r.decide((1, 2, 3), (_ok(1), _ok(2), bad)) == "refused"


def test_decide_one_bad_evidence_refuses_the_batch() -> None:
    bad = r.RowVerdict(
        3, "d2_filled_root", {}, r.EvidenceVerdict("evidence_status_not_filled")
    )
    assert r.decide((1, 2, 3), (_ok(1), _ok(2), bad)) == "refused"


def test_decide_row_eligible_without_evidence_refuses() -> None:
    assert r.decide((1,), (r.RowVerdict(1, "d2_filled_root"),)) == "refused"


def test_decide_all_already_reconciled_is_noop() -> None:
    assert r.decide((1, 2), (_done(1), _done(2))) == "noop"


def test_decide_mixed_already_and_eligible_is_refused() -> None:
    assert r.decide((1, 2), (_done(1), _ok(2))) == "refused"


def test_decide_empty_or_short_is_refused() -> None:
    assert r.decide((), ()) == "refused"
    assert r.decide((1, 2), (_ok(1),)) == "refused"
    assert r.decide((1, 2), (_done(1),)) == "refused"


# ------------------------------------------------- broker order id (r1 B1)

_MALFORMED_IDS = [
    "0",
    "-1",
    "+7",
    "07",
    " 7",
    "7 ",
    "True",
    "true",
    "None",
    "null",
    "Infinity",
    "NaN",
    "0.0",
    "7.0",
    "1e3",
    "７",
    str(2**63),
    "",
]


@pytest.mark.parametrize("bad", _MALFORMED_IDS)
def test_malformed_stored_broker_order_id_is_not_row_eligible(bad: str) -> None:
    verdict = r.evaluate_row(1, fake_row(broker_order_id=bad), fake_instrument())
    assert verdict.verdict == "broker_order_id_missing"


@pytest.mark.parametrize(
    "bad", [*_MALFORMED_IDS, 0, -1, True, False, 2**63, 7.0, None, [7]]
)
def test_malformed_evidence_order_id_never_matches_even_if_row_agrees(bad) -> None:
    # The tester's exploit: stored id and evidence id agree on a malformed value.
    row = fake_row(broker_order_id=bad if isinstance(bad, str) else "9000001")
    assert not r.evaluate_evidence(row, filled_body(broker_order_id=bad)).matches


@pytest.mark.parametrize("good", ["1", "9000001", str(2**63 - 1)])
def test_canonical_order_ids_match_as_int_or_string(good: str) -> None:
    row = fake_row(broker_order_id=good)
    assert r.evaluate_row(1, row, fake_instrument()).row_eligible
    assert r.evaluate_evidence(row, filled_body(broker_order_id=good)).matches
    assert r.evaluate_evidence(row, filled_body(broker_order_id=int(good))).matches


def test_non_string_client_order_id_never_matches() -> None:
    body = filled_body()
    body["clientOrderId"] = ["d2rem-x"]
    assert r.evaluate_evidence(fake_row(), body).verdict == "evidence_client_order_id"
