"""D8 (ROB-1391): Toss broker-side DAY-sweep recognition in parent expiry.

Operator decision Q-1019-6 = A (hk:doc decision/2026-10-19/rob1391-d8). A Toss
US buy row with ``status='cancelled'`` counts as regular-session DAY expiry
iff every element of the pre-registered sweep signature holds: broker status
``CANCELED``, zero fills, a raw ``canceledAt`` inside [working-session regular
close, next regular open) and within +-2min (both ends inclusive) of
08:50 KST or 19:50 ET wall clock, and no same-ledger cancel row targeting its
broker order id. The live ledger classifier is unchanged.

Fixtures use the production raw shape the desk aggregate enumerated
(hk:doc deploy/2026-10-19/desk-req14-cancel-semantics): ``raw_response`` is
the stored broker payload and ``canceledAt`` is an offset-bearing
millisecond ISO string, e.g. ``"2026-05-05T08:50:00.123+09:00"``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from research.rob1391_us_resubmit_shadow import engine
from research.rob1391_us_resubmit_shadow import parents as parents_mod
from research.rob1391_us_resubmit_shadow.manifest import (
    DEVIATIONS,
    MANDATORY_DEVIATIONS,
    ManifestError,
    build_manifest,
)
from research.rob1391_us_resubmit_shadow.parents import assemble_orders, normalise
from research.rob1391_us_resubmit_shadow.state import ARMS
from research.rob1391_us_resubmit_shadow.us_calendar import calendar_for
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    CAL,
    POLICY,
    SEAL_AT,
    FakeMarket,
    FakeSource,
    context,
    et,
    kis_row,
    rung_for,
    seal,
    toss_row,
    universe_row,
)
from tests.research.rob1391_us_resubmit_shadow.test_mutants import mutant

pytestmark = pytest.mark.unit

D0 = date(2025, 11, 17)  # EST (UTC-5); inside the sealed window
D0_EDT = date(2026, 5, 4)  # EDT (UTC-4); inside helpers.CAL's span
CAL_DST = calendar_for(date(2026, 11, 2))  # spans the 2026-11-01 DST change
D0_PRE_DST = date(2026, 10, 30)  # last EDT session before the change
D0_POST_DST = date(2026, 11, 2)  # first EST session after it

_DEFAULT_RAW = object()


def canceled_row(
    sid: int,
    symbol: str,
    *,
    day: date,
    canceled_at: Any,
    broker_status: str | None = "CANCELED",
    filled: Any = 0,
    avg: Any = None,
    our_cancel: bool = False,
    raw: Any = _DEFAULT_RAW,
    hh: int = 10,
    mm: int = 5,
    price: Any = "99.50",
    qty: Any = 2,
) -> dict[str, Any]:
    """A Toss cancelled US buy with the production ``raw_response`` shape.

    ``canceled_at`` is the raw ``raw_response.canceledAt`` value (the broker
    sends ``"YYYY-MM-DDTHH:MM:SS.sss+09:00"``); ``raw`` overrides the whole
    payload (``None`` = the ledger column is NULL).
    """

    ordered = et(day, hh, mm)
    if raw is _DEFAULT_RAW:
        raw = {
            "orderId": f"TOSSORD{sid:08d}",
            "symbol": symbol,
            "side": "BUY",
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "status": broker_status,
            "price": str(price),
            "quantity": str(qty),
            "orderAmount": str(Decimal(str(price)) * Decimal(str(qty))),
            "currency": "USD",
            "orderedAt": ordered.isoformat(),
            "canceledAt": canceled_at,
            "execution": {},
        }
    return toss_row(
        sid,
        symbol,
        ordered_at=ordered,
        price=price,
        qty=qty,
        status="cancelled",
        filled=filled,
        avg=avg,
        broker_status=broker_status,
        raw_response=raw,
        our_cancel_row_exists=our_cancel,
    )


def norm(row, cal=CAL):
    """Default: the order carries its approved, policy-stamped rung."""

    orders = assemble_orders([row], [rung_for(row)], [universe_row(row["symbol"])])
    return normalise(orders[0], cal)


# ------------------------------------------------------------------ accept


def test_kst_sweep_during_edt_is_recognised_with_canceledat_as_expiry():
    # The desk-observed shape: 08:50 KST on the next KST date = 19:50 EDT.
    cand, reasons = norm(
        canceled_row(1, "SYMA", day=D0_EDT, canceled_at="2026-05-05T08:50:00.123+09:00")
    )
    assert reasons == []
    assert cand.expiry_basis == "toss_preday_sweep_inferred"
    assert cand.expiry_at == datetime(2026, 5, 4, 23, 50, 0, 123000, tzinfo=UTC)
    assert cand.expiry_at == datetime.fromisoformat("2026-05-05T08:50:00.123+09:00")


def test_kst_signature_during_est_is_recognised():
    # 08:50 KST = 18:50 ET under EST -- the KST signature still applies.
    cand, reasons = norm(
        canceled_row(2, "SYMA", day=D0, canceled_at="2025-11-18T08:50:00.000+09:00")
    )
    assert reasons == [] and cand.expiry_basis == "toss_preday_sweep_inferred"
    assert cand.expiry_at == datetime(2025, 11, 17, 23, 50, tzinfo=UTC)


def test_et_signature_during_est_is_recognised_in_either_notation():
    # 19:50 ET under EST; +09:00 and -05:00 spellings of the same instant are
    # equivalent evidence.
    for raw in ("2025-11-18T09:50:00.000+09:00", "2025-11-17T19:50:00.000-05:00"):
        cand, reasons = norm(canceled_row(3, "SYMA", day=D0, canceled_at=raw))
        assert reasons == []
        assert cand.expiry_at == datetime(2025, 11, 18, 0, 50, tzinfo=UTC)


def test_z_notation_and_json_text_payload_are_recognised():
    # FileParentSource replays hand the column over as text; a "Z" spelling of
    # the sweep instant is the same evidence.
    row = canceled_row(
        4,
        "SYMA",
        day=D0,
        canceled_at=None,
        raw=json.dumps(
            {"orderId": "TOSSORD00000004", "canceledAt": "2025-11-18T00:50:00.000Z"}
        ),
    )
    cand, reasons = norm(row)
    assert reasons == []
    assert cand.expiry_at == datetime(2025, 11, 18, 0, 50, tzinfo=UTC)


def test_sweep_after_an_early_close_is_still_inside_the_gap():
    # 2025-11-28 closes 13:00 ET (early close); the next-day 08:50 KST sweep
    # still sits inside [close, next open).
    early = date(2025, 11, 28)
    cand, reasons = norm(
        canceled_row(5, "SYMA", day=early, canceled_at="2025-11-29T08:50:00.000+09:00")
    )
    assert reasons == [] and cand.expiry_basis == "toss_preday_sweep_inferred"


def test_dst_boundary_sessions_on_both_sides_are_recognised():
    # Friday 2026-10-30 is still EDT; Monday 2026-11-02 is already EST.
    cand, reasons = norm(
        canceled_row(
            6, "SYMA", day=D0_PRE_DST, canceled_at="2026-10-31T08:50:00.000+09:00"
        ),
        cal=CAL_DST,
    )
    assert reasons == []  # 19:50 EDT
    cand, reasons = norm(
        canceled_row(
            7, "SYMA", day=D0_POST_DST, canceled_at="2026-11-03T08:50:00.000+09:00"
        ),
        cal=CAL_DST,
    )
    assert reasons == []  # 18:50 EST -- KST signature
    cand, reasons = norm(
        canceled_row(
            8, "SYMA", day=D0_POST_DST, canceled_at="2026-11-03T09:50:00.000+09:00"
        ),
        cal=CAL_DST,
    )
    assert reasons == []  # 19:50 EST -- ET signature


# ------------------------------------------------------- signature bounds


@pytest.mark.parametrize(
    "canceled_at",
    [
        "2026-05-05T08:48:00.000+09:00",  # -120.000s: inside, bound inclusive
        "2026-05-05T08:52:00.000+09:00",  # +120.000s: inside, bound inclusive
        "2026-05-04T19:49:00.000-04:00",  # -60s of the ET target: inside
        "2026-05-04T19:50:00.001-04:00",  # +1ms: inside
    ],
)
def test_signature_tolerance_inclusive_bounds_accept(canceled_at):
    cand, reasons = norm(canceled_row(9, "SYMA", day=D0_EDT, canceled_at=canceled_at))
    assert reasons == [] and cand.expiry_basis == "toss_preday_sweep_inferred"


@pytest.mark.parametrize(
    "canceled_at",
    [
        "2026-05-05T08:47:59.999+09:00",  # -120.001s: just outside
        "2026-05-05T08:52:00.001+09:00",  # +120.001s: just outside
        "2026-05-04T19:44:00.000-04:00",  # -6min: in-window, no signature
        "2026-05-04T22:50:00.000-04:00",  # 22:50 ET: in-window, no signature
    ],
)
def test_signature_outside_tolerance_rejects(canceled_at):
    cand, reasons = norm(canceled_row(10, "SYMA", day=D0_EDT, canceled_at=canceled_at))
    assert cand is None
    assert reasons == ["not_day_expiry_cancelled_signature"]


# --------------------------------------------------------- window bounds


@pytest.mark.parametrize(
    ("canceled_at", "reason"),
    [
        # 15:59 ET -- before the working session's regular close.
        ("2025-11-17T15:59:00.000-05:00", "not_day_expiry_cancelled_outside_window"),
        # exactly at the next regular open: the window is [close, open).
        ("2025-11-18T14:30:00.000Z", "not_day_expiry_cancelled_outside_window"),
        # signature-matching but a day late (next evening's 19:50 ET).
        ("2025-11-19T09:50:00.000+09:00", "not_day_expiry_cancelled_outside_window"),
        # inside the gap but off both signatures (17:30 ET).
        ("2025-11-17T17:30:00.000-05:00", "not_day_expiry_cancelled_signature"),
    ],
)
def test_window_and_signature_rejects(canceled_at, reason):
    cand, reasons = norm(canceled_row(11, "SYMA", day=D0, canceled_at=canceled_at))
    assert cand is None and reasons == [reason]


def test_at_exact_regular_close_is_in_window_but_fails_signature():
    # 16:00 ET = the close itself -> inside the gap, then no signature match.
    cand, reasons = norm(
        canceled_row(12, "SYMA", day=D0, canceled_at="2025-11-18T06:00:00.000+09:00")
    )
    assert cand is None and reasons == ["not_day_expiry_cancelled_signature"]


# ------------------------------------------------------------ rejections


def test_our_own_cancel_row_rejects_the_sweep_inference():
    cand, reasons = norm(
        canceled_row(
            13,
            "SYMA",
            day=D0,
            canceled_at="2025-11-18T08:50:00.000+09:00",
            our_cancel=True,
        )
    )
    assert cand is None and reasons == ["not_day_expiry_cancelled_own_cancel_row"]


def test_missing_or_unparseable_canceledat_is_unknown_not_a_guess():
    for value in (
        None,  # canceledAt absent/null
        "not-an-iso-instant",  # unparseable
        "2025-11-18T08:50:00.000",  # naive: no offset -> not assumed KST
        "",  # empty
        12345,  # non-string scalar
    ):
        cand, reasons = norm(canceled_row(14, "SYMA", day=D0, canceled_at=value))
        assert cand is None and reasons == ["toss_cancel_timestamp_missing"], value
    # a NULL raw_response column, an empty payload and a payload without the
    # key are all the same unknown
    for raw in (None, {}, {"orderId": "TOSSORD00000015"}):
        cand, reasons = norm(
            canceled_row(15, "SYMA", day=D0, canceled_at=None, raw=raw)
        )
        assert cand is None and reasons == ["toss_cancel_timestamp_missing"], raw


def test_partial_fill_is_not_a_clean_sweep():
    cand, reasons = norm(
        canceled_row(
            18,
            "SYMA",
            day=D0,
            canceled_at="2025-11-18T08:50:00.000+09:00",
            filled=1,
            avg="99.40",
            qty=3,
        )
    )
    assert cand is None and reasons == ["not_day_expiry_cancelled_partial_fill"]


def test_wrong_broker_status_rejects_and_multiple_failures_list_all():
    for broker_status in ("REJECTED", "FILLED", "Canceled", None):
        cand, reasons = norm(
            canceled_row(
                19,
                "SYMA",
                day=D0,
                canceled_at="2025-11-18T08:50:00.000+09:00",
                broker_status=broker_status,
            )
        )
        assert cand is None
        assert "not_day_expiry_cancelled_broker_status" in reasons
    cand, reasons = norm(
        canceled_row(
            20,
            "SYMA",
            day=D0,
            canceled_at="2025-11-18T07:30:00.000+09:00",  # in-window, off-signature
            broker_status="REJECTED",
            filled=1,
            avg="99.40",
            qty=3,
            our_cancel=True,
        )
    )
    assert cand is None
    assert reasons == [
        "not_day_expiry_cancelled_broker_status",
        "not_day_expiry_cancelled_partial_fill",
        "not_day_expiry_cancelled_signature",
        "not_day_expiry_cancelled_own_cancel_row",
    ]


def test_nested_only_canceledat_is_found_but_ambiguous_values_fail_closed():
    # the desk counted with $.**.canceledAt -- nested placements qualify
    row = canceled_row(
        21,
        "SYMA",
        day=D0,
        canceled_at=None,
        raw={
            "orderId": "TOSSORD00000021",
            "cancelResult": {"order": {"canceledAt": "2025-11-18T08:50:00.000+09:00"}},
        },
    )
    cand, reasons = norm(row)
    assert reasons == []
    assert cand.expiry_at == datetime(2025, 11, 17, 23, 50, tzinfo=UTC)
    # two disagreeing occurrences -> ambiguous -> fail closed
    row["raw_response"] = {
        "canceledAt": "2025-11-18T08:50:00.000+09:00",
        "cancelResult": {"canceledAt": "2025-11-18T08:55:00.000+09:00"},
    }
    cand, reasons = norm(row)
    assert cand is None and reasons == ["toss_cancel_timestamp_missing"]
    # agreeing occurrences (same instant, different spellings) still qualify
    row["raw_response"] = {
        "canceledAt": "2025-11-18T08:50:00.000+09:00",
        "cancelResult": {"canceledAt": "2025-11-17T23:50:00.000Z"},
    }
    cand, reasons = norm(row)
    assert reasons == []


# ------------------------------------------------- existing paths unchanged


def test_existing_expiry_paths_are_unchanged():
    cand, _ = norm(toss_row(30, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2))
    assert cand is not None and cand.expiry_basis == "toss_expired_at"
    row = toss_row(
        31,
        "SYMA",
        ordered_at=et(D0, 10, 5),
        price="99.50",
        qty=2,
        status="expired",
    )
    row["expired_at"] = None
    cand, reasons = norm(row)
    assert cand is None and reasons == ["expiry_timestamp_missing"]
    cand, reasons = norm(
        toss_row(
            32,
            "SYMA",
            ordered_at=et(D0, 10, 5),
            price="99.50",
            qty=2,
            status="rejected",
        )
    )
    assert cand is None and reasons == ["not_day_expiry_rejected"]
    # a bare cancelled row keeps failing closed -- nothing is guessed
    cand, reasons = norm(
        toss_row(
            33,
            "SYMA",
            ordered_at=et(D0, 10, 5),
            price="99.50",
            qty=2,
            status="cancelled",
        )
    )
    assert cand is None
    assert reasons == [
        "not_day_expiry_cancelled_broker_status",
        "toss_cancel_timestamp_missing",
    ]
    # KIS rows never take the Toss path
    cand, reasons = norm(
        kis_row(34, "SYMB", ordered_at=et(D0, 9, 45), price="412.10", qty=1, filled="0")
    )
    assert reasons == [] and cand.expiry_basis == "kis_day_close_inferred"
    cand, reasons = norm(
        kis_row(
            35,
            "SYMB",
            ordered_at=et(D0, 9, 45),
            price="412.10",
            qty=1,
            status="cancelled",
        )
    )
    assert cand is None and reasons == ["not_day_expiry_cancelled"]


# --------------------------------------------- register: both arms, one expiry


def test_sweep_parent_registers_one_expiry_shared_by_both_arms(tmp_path):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    row = canceled_row(40, "SYMA", day=D0, canceled_at="2025-11-18T08:50:00.123+09:00")
    src.order_rows.append(row)
    src.rung_rows.append(rung_for(row))
    src.universe_rows.append(universe_row("SYMA"))
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    (parent,) = ctx.state.parents.values()
    # eligibility runs before arm assignment: one parent record, one expiry
    assert parent.rec["expiry_basis"] == "toss_preday_sweep_inferred"
    assert datetime.fromisoformat(parent.rec["expiry_at"]) == datetime(
        2025, 11, 17, 23, 50, 0, 123000, tzinfo=UTC
    )
    assert set(parent.arms) == set(ARMS)


def test_rejected_sweep_registers_no_parent(tmp_path):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    row = canceled_row(
        41,
        "SYMA",
        day=D0,
        canceled_at="2025-11-18T12:50:00.000+09:00",  # 22:50 ET, off-signature
    )
    src.order_rows.append(row)
    src.rung_rows.append(rung_for(row))
    src.universe_rows.append(universe_row("SYMA"))
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    assert ctx.state.parents == {}
    excl = ctx.state.registered[D0.isoformat()]["exclusions"]
    assert [e["reasons"] for e in excl] == [["not_day_expiry_cancelled_signature"]]


# --------------------------------------------------------------- deviation


def test_d8_is_a_mandatory_sealed_deviation(tmp_path):
    assert "D8_toss_preday_cancel_sweep" in MANDATORY_DEVIATIONS
    mandatory, rule = DEVIATIONS["D8_toss_preday_cancel_sweep"]
    assert mandatory is True
    assert "toss_preday_sweep_inferred" in rule and "unchanged" in rule
    # seal refuses when D8 approval is missing (same device as D1-D7)
    prereg = tmp_path / "p.md"
    prereg.write_text("x")
    kwargs = {
        "seal_at": SEAL_AT,
        "approval_ref": "ok",
        "prereg_path": prereg,
        "code_sha": "abc",
        "policy_stamp": dict(POLICY),
        "deployed_policy": dict(POLICY),
        "feed": "sip",
        "fee_schedule_path": None,
        "deviation_ref": "hk:doc decision/2026-10-19/rob1391-d8",
    }
    with pytest.raises(ManifestError, match="D8_toss_preday_cancel_sweep"):
        build_manifest(
            **kwargs,
            approved_deviations={
                "D1_exit_hold_to_d20",
                "D2_resubmit_gate_subset",
                "D7_in_window_quote_evidence",
            },
        )
    m = build_manifest(**kwargs, approved_deviations=MANDATORY_DEVIATIONS)
    assert "D8_toss_preday_cancel_sweep" in m["deviations"]["approved"]


# ------------------------------------------------------------- mutant RED


def _sweep_or_reject(mod, row):
    o = mod.assemble_orders([row], [rung_for(row)], [universe_row(row["symbol"])])[0]
    return mod.normalise(o, CAL)


def check_own_cancel_guard(p) -> None:
    cand, reasons = _sweep_or_reject(
        p,
        canceled_row(
            50,
            "SYMA",
            day=D0,
            canceled_at="2025-11-18T08:50:00.000+09:00",
            our_cancel=True,
        ),
    )
    assert cand is None
    assert reasons == ["not_day_expiry_cancelled_own_cancel_row"]


def check_signature_guard(p) -> None:
    cand, reasons = _sweep_or_reject(
        p,
        canceled_row(
            51,
            "SYMA",
            day=D0,
            canceled_at="2025-11-18T07:30:00.000+09:00",  # in-window, no signature
        ),
    )
    assert cand is None
    assert reasons == ["not_day_expiry_cancelled_signature"]


def check_window_guard(p) -> None:
    cand, reasons = _sweep_or_reject(
        p,
        canceled_row(
            52,
            "SYMA",
            day=D0,
            canceled_at="2025-11-19T09:50:00.000+09:00",  # signature ok, day late
        ),
    )
    assert cand is None
    assert reasons == ["not_day_expiry_cancelled_outside_window"]


def test_sweep_guard_checks_pass_on_real_module():
    check_own_cancel_guard(parents_mod)
    check_signature_guard(parents_mod)
    check_window_guard(parents_mod)


D8_MUTANTS = [
    (
        "own_cancel_row_check_removed",
        "    if o.our_cancel_row_exists:\n",
        "    if False:\n",
        check_own_cancel_guard,
    ),
    (
        "signature_check_removed",
        "    elif not _toss_sweep_signature(o.broker_canceled_at):\n",
        "    elif False:\n",
        check_signature_guard,
    ),
    (
        "window_check_removed",
        "    elif not (session.close_utc <= o.broker_canceled_at < nxt.open_utc):\n",
        "    elif False:\n",
        check_window_guard,
    ),
]


@pytest.mark.parametrize(
    ("name", "old", "new", "check"), D8_MUTANTS, ids=[m[0] for m in D8_MUTANTS]
)
def test_d8_guard_mutants_are_red(monkeypatch, name, old, new, check):
    """Removing any D8 guard must turn its check RED via AssertionError."""

    mods = mutant(monkeypatch, "parents", old, new)
    with pytest.raises(AssertionError):
        check(mods["parents"])
