"""register / step state machine: order, idempotency, caps, latches, UNKNOWN."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow import engine
from research.rob1391_us_resubmit_shadow.state import ArmBook, ParentState
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    CAL,
    FakeMarket,
    FakeSource,
    bar_json,
    context,
    et,
    quote_json,
    rung_row,
    seal,
    toss_row,
    universe_row,
)

pytestmark = pytest.mark.unit

D0 = date(2025, 11, 17)
D1 = CAL.offset(D0, 1)


def setup(tmp_path, **kw):
    ledger = seal(tmp_path, **kw)
    return ledger, FakeSource(), FakeMarket()


def add_parent(
    src,
    mkt,
    sid,
    sym,
    *,
    day=D0,
    price="99.50",
    qty=3,
    filled=0,
    avg=None,
    notional=None,
    hh=8,
    mm=5,
    status="expired",
):
    row = toss_row(
        sid,
        sym,
        ordered_at=et(day, hh, mm),
        price=price,
        qty=qty,
        filled=filled,
        avg=avg,
        status=status,
    )
    src.order_rows.append(row)
    if notional is not None:
        src.rung_rows.append(
            rung_row(
                row["broker_order_id"],
                qty=qty,
                price=price,
                notional=notional,
                root=f"root-{sid}",
            )
        )
    if not any(u["symbol"] == sym for u in src.universe_rows):
        src.universe_rows.append(universe_row(sym))
    mkt.price.setdefault(sym, 100.0)
    return row


def trade_through(mkt, sym, day, *, minute=30, low=99.49, v=50_000, ask=99.50):
    sess = CAL.session(day)
    bars = list(mkt._session_minutes(sym, day))
    t = sess.open_utc + timedelta(minutes=minute)
    bars[minute] = bar_json(t, o=99.6, h=99.7, low=low, c=99.55, v=v)
    mkt.minutes[(sym, day)] = bars
    mkt.quotes[(sym, day)] = [
        quote_json(sess.open_utc + timedelta(seconds=1), bid=99.98, ask=100.00),
        quote_json(t + timedelta(seconds=5), bid=round(ask - 0.01, 2), ask=ask),
    ]


def events_bytes(ledger):
    return ledger.events_path.read_bytes()


# ------------------------------------------------------------------ ordering


def test_strict_session_order_and_idempotency(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    ctx = context(ledger, src, mkt)
    with pytest.raises(engine.OrderingError):
        engine.register(ctx, date(2025, 11, 14))  # step(11-14) not done yet
    engine.step(ctx, date(2025, 11, 14))
    engine.register(ctx, date(2025, 11, 14))
    before = events_bytes(ledger)
    assert engine.register(ctx, date(2025, 11, 14)) is None  # rerun = no change
    assert engine.step(ctx, date(2025, 11, 14)) is None
    assert events_bytes(ledger) == before
    with pytest.raises(engine.OrderingError):
        engine.step(ctx, date(2025, 11, 18))  # skips 11-17


def test_look_ahead_guard_refuses_before_close(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    ctx = context(
        ledger,
        src,
        mkt,
        now=CAL.session(date(2025, 11, 14)).close_utc + timedelta(minutes=10),
    )
    with pytest.raises(engine.OrderingError, match="look-ahead"):
        engine.step(ctx, date(2025, 11, 14))


def test_window_boundary_register_21st_session_refused_step_continues(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    ctx = context(ledger, src, mkt)
    window = ctx.state.window
    assert len(window) == K.WINDOW_SESSIONS
    engine.advance(ctx, date.fromisoformat(window[-1]))
    assert set(ctx.state.registered) == set(window)
    after = CAL.offset(date.fromisoformat(window[-1]), 1)
    with pytest.raises(engine.OrderingError, match="outside"):
        engine.register(ctx, after)
    assert engine.step(ctx, after) is not None


# ------------------------------------------------------------------ assignment


def test_registered_parent_and_virtual_fill_flow(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", qty=3, filled=1, avg="99.40", notional="310")
    trade_through(mkt, "SYMA", D1)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D1)
    (parent,) = ctx.state.parents.values()
    assert (
        parent.rec["d0"] == "2025-11-17"
        and parent.rec["d20"] == CAL.offset(D0, 20).isoformat()
    )
    assert parent.rec["resubmit_sessions"] == [
        CAL.offset(D0, k).isoformat() for k in (1, 2, 3)
    ]
    assert parent.arms["C"].shares == 1  # real D0 fill only, never virtual
    assert parent.arms["T"].shares == 3  # 1 real + 2 virtual at D1
    assert {
        Decimal(f["price"]) for f in parent.arms["T"].fills if f["kind"] == "virtual"
    } == {Decimal("99.50")}
    assert not ctx.state.latched


def test_daily_cap_three_and_order_by_expiry_then_episode(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    rows = [
        add_parent(src, mkt, i, f"SYM{i}", qty=1, price="20.00") for i in range(1, 6)
    ]
    # distinct expiry instants: later rows expire earlier
    for i, row in enumerate(rows):
        row["expired_at"] = row["expired_at"] - timedelta(minutes=i)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    payload = ctx.state.registered[D0.isoformat()]
    assigned = [r["symbol"] for r in payload["assigned"]]
    assert assigned == ["SYM5", "SYM4", "SYM3"]
    capped = sorted(
        e["symbol"] for e in payload["exclusions"] if e["reasons"] == ["daily_cap"]
    )
    assert capped == ["SYM1", "SYM2"]


def _dummy_parent(i: int, d0: date) -> ParentState:
    rec = {
        "episode_id": f"ep-{i:016d}",
        "source_ref": f"toss_live_order_ledger:old{i}",
        "symbol": f"OLD{i}",
        "d0": d0.isoformat(),
        "d20": CAL.offset(d0, 20).isoformat(),
        "budget": "10",
        "q": 1,
        "assignment_seq": i,
        "resubmit_sessions": [],
        "last_tracked": CAL.offset(d0, 23).isoformat(),
    }
    return ParentState(rec=rec, arms={a: ArmBook() for a in ("C", "T", "T_opt")})


@pytest.mark.parametrize(("existing", "expected"), [(59, 1), (60, 0)])
def test_total_cap_sixty(tmp_path, existing, expected):
    ledger, src, mkt = setup(tmp_path)
    ctx = context(ledger, src, mkt)
    old = CAL.offset(D0, -25)
    for i in range(existing):
        ctx.state.parents[f"ep-{i:016d}"] = _dummy_parent(i, old)
    cands = []
    for i in range(2):
        row = add_parent(src, mkt, 900 + i, f"NEW{i}", qty=1, price="20.00")
        from research.rob1391_us_resubmit_shadow.parents import (
            assemble_orders,
            build_episodes,
            normalise,
        )

        o = assemble_orders([row], [], [universe_row(f"NEW{i}")])[0]
        cands.append((normalise(o, CAL)[0], o))
    episodes = build_episodes([o for _, o in cands], CAL)
    eligible = [(c, episodes.episode_of[o.source_ref]) for c, o in cands]
    assigned, log = engine._assign(ctx, D0, eligible, episodes, {})
    assert len(assigned) == expected
    if expected == 0:
        assert {tuple(e["reasons"]) for e in log} == {("total_cap",)}


def test_virtual_cash_reservation(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    for i in range(1, 4):
        add_parent(src, mkt, i, f"SYM{i}", qty=10, price="400.00", hh=8, mm=i)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    payload = ctx.state.registered[D0.isoformat()]
    assert len(payload["assigned"]) == 2  # 2 x 4000 reserved of E0 10000
    assert [e["reasons"] for e in payload["exclusions"]] == [
        ["insufficient_virtual_cash"]
    ]


def test_dedupe_member_is_not_a_second_parent_and_budget_counted_once(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", day=D0, price="99.50", qty=2)
    add_parent(src, mkt, 2, "SYMA", day=CAL.offset(D0, 2), price="98.00", qty=2)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, CAL.offset(D0, 2))
    assert len(ctx.state.parents) == 1
    excl = ctx.state.registered[CAL.offset(D0, 2).isoformat()]["exclusions"]
    assert [e["reasons"] for e in excl] == [["episode_member_not_anchor"]]
    (parent,) = ctx.state.parents.values()
    assert parent.budget == Decimal("199.00")
    # not visible when D0 registered (no look-ahead); linked by the later step
    assert parent.rec["linked_refs"] == []
    assert parent.real_followups == ["toss_live_order_ledger:2"]


def test_symbol_with_open_parent_blocks_new_episode(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", day=D0, price="99.50", qty=2)
    later = CAL.offset(D0, 8)  # new episode (gap > 5) but D+20 not reached
    add_parent(src, mkt, 2, "SYMA", day=later, price="99.50", qty=2)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, later)
    excl = ctx.state.registered[later.isoformat()]["exclusions"]
    assert [e["reasons"] for e in excl] == [["symbol_has_open_parent"]]


def test_pre_seal_order_never_a_parent(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(
        src, mkt, 1, "SYMA", day=date(2025, 11, 14), hh=6, mm=0
    )  # before the 12:00Z seal
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, date(2025, 11, 14))
    excl = ctx.state.registered["2025-11-14"]["exclusions"]
    assert [e["reasons"] for e in excl] == [["pre_seal_order"]]


def test_pending_reconcile_then_quarantine_after_deadline(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", status="accepted")
    src.order_rows[0]["expired_at"] = None
    ctx = context(ledger, src, mkt, now=CAL.session(D0).close_utc + timedelta(hours=2))
    engine.advance(ctx, date(2025, 11, 14))
    engine.step(ctx, D0)
    with pytest.raises(engine.PendingReconcile):
        engine.register(ctx, D0)
    ctx.clock["now"] = CAL.session(D1).close_utc + timedelta(hours=1)
    engine.register(ctx, D0)
    excl = ctx.state.registered[D0.isoformat()]["exclusions"]
    assert [e["reasons"] for e in excl] == [["unreconciled_at_deadline"]]


def test_liquidity_filter_excludes_from_both_arms(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "THIN", qty=2, price="8.95")
    mkt.price["THIN"] = 9.0  # 9 x 1,000,000 = $9m < $10m; spread 22bp passes
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    excl = ctx.state.registered[D0.isoformat()]["exclusions"]
    assert excl[0]["reasons"] == ["liquidity_below_min"] and ctx.state.parents == {}


def test_spread_gate_at_original_submission(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "WIDE")
    sess = CAL.session(D0)
    mkt.quotes[("WIDE", D0)] = [
        quote_json(sess.open_utc + timedelta(seconds=2), bid=99.60, ask=100.00)
    ]
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    assert ctx.state.registered[D0.isoformat()]["exclusions"][0]["reasons"] == [
        "spread_gate_at_submission"
    ]


# ------------------------------------------------------------------ resubmission gates / UNKNOWN


def test_spread_gate_at_resubmission_skips_session_without_extending(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", qty=2)
    trade_through(mkt, "SYMA", D1)
    sess = CAL.session(D1)
    mkt.quotes[("SYMA", D1)][0] = quote_json(
        sess.open_utc + timedelta(seconds=1), bid=99.50, ask=100.00
    )
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, CAL.offset(D0, 4))
    (parent,) = ctx.state.parents.values()
    log = parent.session_log
    assert log[D1.isoformat()]["resubmission"]["T"]["reason"] == "spread_gate"
    assert parent.arms["T"].shares == 0
    assert CAL.offset(D0, 4).isoformat() not in parent.resubmit_sessions
    assert "resubmission" not in log[CAL.offset(D0, 4).isoformat()]


def test_unknown_ask_marks_parent_unknown_and_is_not_zero(tmp_path):
    from research.rob1391_us_resubmit_shadow.report import build_report

    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", qty=2)
    trade_through(mkt, "SYMA", D1)
    mkt.unknown.add(("ask", "SYMA", D1))
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, CAL.offset(D0, 3))
    (parent,) = ctx.state.parents.values()
    assert parent.arms["T"].unknown and parent.arms["T"].unknown.startswith(
        "ask_unobservable"
    )
    d2 = parent.session_log[CAL.offset(D0, 2).isoformat()]["resubmission"]["T"]
    assert d2["reason"] == "prior_unknown"
    assert build_report(ctx.state)["counts"]["unknown_T"] == 1


def test_missing_first_quote_is_unknown(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", qty=2)
    mkt.unknown.add(("quote", "SYMA", D1))
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D1)
    (parent,) = ctx.state.parents.values()
    assert parent.arms["T"].unknown.startswith("first_quote_unavailable")


def test_split_inside_window_blocks_and_adjusts_marks_without_look_ahead(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SPLT", qty=2, filled=1, avg="99.40")
    d2 = CAL.offset(D0, 2)
    # 2:1 split effective D2: raw halves from D2, split-adjusted halves before D2
    for day in [CAL.offset(D0, k) for k in range(-25, 30)]:
        raw = 100.0 if day < d2 else 50.0
        mkt.daily_override[("SPLT", "raw", day)] = bar_json(
            et(day, 0, 0), o=raw, h=raw, low=raw, c=raw, v=1_000_000
        )
        mkt.daily_override[("SPLT", "split", day)] = bar_json(
            et(day, 0, 0), o=50.0, h=50.0, low=50.0, c=50.0, v=2_000_000
        )
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, CAL.offset(D0, 3))
    (parent,) = ctx.state.parents.values()
    d1_entry = parent.session_log[D1.isoformat()]
    assert (
        d1_entry["split_factor_since_d0"] == "1"
    )  # split after D1 is not visible at D1
    d2_entry = parent.session_log[d2.isoformat()]
    assert Decimal(d2_entry["split_factor_since_d0"]) == 2
    assert Decimal(d2_entry["close"]) == Decimal("100")  # 50 x 2 shares per D0 share
    assert d2_entry["resubmission"]["T"]["reason"] == "corporate_action_review"
    assert parent.review_block == "corporate_action"


# ------------------------------------------------------------------ latches


def test_policy_change_latch_persists_and_stops_everything(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(src, mkt, 1, "SYMA", qty=2)
    trade_through(mkt, "SYMA", D1)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    changed = context(
        ledger,
        src,
        mkt,
        policy={"version": "2026-10-20.1", "content_hash": "abcdefabcdef"},
    )
    engine.step(changed, D1)
    assert changed.state.latch_types() == {"policy_change"}
    # a fresh process with the original policy still sees the latch
    again = context(ledger, src, mkt)
    assert again.state.latched
    (parent,) = again.state.parents.values()
    assert (
        parent.session_log[D1.isoformat()]["resubmission"]["T"]["reason"] == "latched"
    )
    add_parent(src, mkt, 2, "SYMB", day=D1)
    engine.register(again, D1)
    assert again.state.registered[D1.isoformat()]["assigned"] == []
    assert again.state.registered[D1.isoformat()]["exclusions"][0]["reasons"] == [
        "latched"
    ]


def test_write_path_reachable_latches(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    ctx = context(
        ledger, src, mkt, modules={"app.services.brokers.toss.client": object()}
    )
    engine.step(ctx, date(2025, 11, 14))
    assert [x["reason"] for x in ctx.state.latches] == ["write_path_reachable"]


def test_reconciliation_mismatch_latches(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    row = add_parent(src, mkt, 1, "SYMA", qty=2)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    row["filled_qty"] = "1.00000000"
    row["avg_fill_price"] = "99.50000000"
    engine.step(ctx, D1)
    assert "reconciliation_mismatch" in {x["reason"] for x in ctx.state.latches}


def test_loss_latch_freezes_books_but_marks_continue(tmp_path):
    ledger, src, mkt = setup(tmp_path)
    add_parent(
        src, mkt, 1, "DROP", qty=3, filled=2, avg="99.50", price="99.50", notional="300"
    )
    for day in [CAL.offset(D0, k) for k in range(1, 30)]:
        mkt.daily_override[("DROP", "raw", day)] = bar_json(
            et(day, 0, 0), o=90, h=90, low=90, c=90, v=1_000_000
        )
    trade_through(mkt, "DROP", CAL.offset(D0, 2), ask=99.50)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, CAL.offset(D0, 5))
    assert ctx.state.latch_types() == {"loss"}
    assert ctx.state.latches[0]["reason"] == "parent_net_loss_ge_5pct_budget"
    (parent,) = ctx.state.parents.values()
    assert parent.arms["T"].shares == 2  # D2 resubmission did not happen
    assert (
        parent.session_log[CAL.offset(D0, 2).isoformat()]["resubmission"]["T"]["reason"]
        == "latched"
    )
    assert (
        Decimal(parent.marks[CAL.offset(D0, 5).isoformat()]) == 90
    )  # observation continues


def test_unknown_latch_type_or_unlatch_kind_cannot_be_appended(tmp_path):
    from research.rob1391_us_resubmit_shadow.ledger import LedgerIntegrityError

    ledger, _, _ = setup(tmp_path)
    with pytest.raises(LedgerIntegrityError):
        ledger.append("unlatch", {"reason": "operator"}, now=engine.now_utc())
    with pytest.raises(LedgerIntegrityError):
        ledger.append("latch", {"latch_type": "released"}, now=engine.now_utc())
