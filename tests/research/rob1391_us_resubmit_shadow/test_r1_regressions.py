"""Regressions for tester round 1 (verdict-r1, head f8af051): F1-F9, S1.

Each test reproduces the reported scenario and asserts the fail-closed result.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from research.rob1391_us_resubmit_shadow import engine, market_data
from research.rob1391_us_resubmit_shadow import report as R
from research.rob1391_us_resubmit_shadow.costs import FeeSchedule, FeeScheduleError
from research.rob1391_us_resubmit_shadow.fills import simulate_conservative
from research.rob1391_us_resubmit_shadow.ledger import Ledger
from research.rob1391_us_resubmit_shadow.manifest import ManifestError, build_manifest
from research.rob1391_us_resubmit_shadow.market_data import (
    OK,
    UNKNOWN,
    AskEvidence,
    parse_bar,
)
from research.rob1391_us_resubmit_shadow.parents import assemble_orders, normalise
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    CAL,
    FEE_SCHEDULE,
    POLICY,
    SEAL_AT,
    FakeMarket,
    FakeSource,
    bar_json,
    context,
    et,
    quote_json,
    rung_for,
    seal,
    toss_row,
    universe_row,
)

pytestmark = pytest.mark.unit

D0 = date(2025, 11, 17)
D1 = CAL.offset(D0, 1)


def _parent(
    src,
    mkt,
    sid=1,
    sym="SYMA",
    *,
    day=D0,
    qty=2,
    filled=0,
    avg=None,
    price="99.50",
    notional=None,
):
    row = toss_row(
        sid,
        sym,
        ordered_at=et(day, 10, 5),
        price=price,
        qty=qty,
        filled=filled,
        avg=avg,
    )
    src.order_rows.append(row)
    src.rung_rows.append(rung_for(row, notional=notional))
    src.universe_rows.append(universe_row(sym))
    mkt.price.setdefault(sym, 100.0)
    return row


def _trade_through(mkt, sym, day, minute=30):
    sess = CAL.session(day)
    bars = list(mkt._session_minutes(sym, day))
    t = sess.open_utc + timedelta(minutes=minute)
    bars[minute] = bar_json(t, o=99.6, h=99.7, low=99.40, c=99.55, v=50_000)
    mkt.minutes[(sym, day)] = bars
    quotes = list(mkt._session_quotes(sym, day))
    quotes.append(quote_json(t + timedelta(seconds=5), bid=99.49, ask=99.50))
    mkt.quotes[(sym, day)] = sorted(quotes, key=lambda q: q["t"])


# ---------------------------------------------------------------- F1 redirects


class _Resp:
    def __init__(self, status, payload=None, url=None):
        self.status_code, self._payload, self.url = status, payload or {}, url

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses, self.kwargs = list(responses), []

    def get(self, url, params=None, timeout=None, **kw):
        self.kwargs.append(kw)
        self.params = getattr(self, "params", []) + [dict(params or {})]
        return self.responses.pop(0)


def _client(responses):
    sess = _Session(responses)
    return market_data.AlpacaMarketData(
        feed="sip", sink=lambda *a: "d", session=sess, min_interval_sec=0
    ), sess


T0 = datetime(2025, 11, 18, 14, 30, tzinfo=UTC)
T1 = datetime(2025, 11, 18, 21, 0, tzinfo=UTC)


def test_f1_redirects_are_not_followed_and_are_unknown():
    c, sess = _client([_Resp(302, url="https://data.alpaca.markets/v2/stocks/bars")])
    res = c.minute_bars("SYMA", T0, T1)
    assert res.status == UNKNOWN and res.reason == "redirect_refused"
    assert sess.kwargs == [{"allow_redirects": False}]


def test_f1_response_from_another_host_is_refused():
    c, _ = _client(
        [_Resp(200, {"bars": {"SYMA": []}}, url="https://outside.invalid/capture")]
    )
    assert c.minute_bars("SYMA", T0, T1).reason == "redirect_refused"


# ---------------------------------------------------------------- F2 missing data


@pytest.mark.parametrize(
    "payload",
    [{}, {"bars": []}, {"bars": {"SYMA": [1]}}, {"bars": {"SYMA": {"t": 1}}}],
    ids=["missing_key", "list_block", "non_dict_row", "non_list_rows"],
)
def test_f2_malformed_payload_is_unknown(payload):
    c, _ = _client([_Resp(200, payload)])
    assert c.minute_bars("SYMA", T0, T1).status == UNKNOWN


def test_f2_empty_quote_stream_is_unknown_not_skip():
    c, _ = _client([_Resp(200, {"quotes": {"SYMA": []}, "next_page_token": None})])
    assert c.first_valid_quote("SYMA", T0, T1).status == UNKNOWN
    c, _ = _client([_Resp(200, {"quotes": {"SYMA": []}, "next_page_token": None})])
    ev = c.ask_at_or_below("SYMA", T0, T0 + timedelta(minutes=1), Decimal("99.50"))
    assert ev.status == UNKNOWN and ev.reason == "no_valid_quote_in_window"


def test_f2_d7_pre_window_quote_is_never_evidence():
    """D7 (operator Q-1018-4 = A): a quote before the window is not carried
    forward, even when its ask is at/below the limit."""

    end = T0 + timedelta(minutes=1)
    rows = [quote_json(T0 - timedelta(seconds=30), bid=99.49, ask=99.50)]
    ev = market_data.ask_evidence_from_quotes(rows, T0, end, Decimal("99.50"), True)
    assert ev.status == UNKNOWN and ev.reason == "no_valid_quote_in_window"
    rows.append(quote_json(T0 + timedelta(seconds=5), bid=99.50, ask=99.60))
    ev = market_data.ask_evidence_from_quotes(rows, T0, end, Decimal("99.50"), True)
    assert ev.status == OK and not ev.found  # valid in-window quote, above P


@pytest.mark.parametrize(
    "case",
    ["empty_minutes", "empty_first_quote", "missing_volume", "missing_daily_bar"],
)
def test_f2_missing_session_data_makes_t_unknown(tmp_path, case):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    _parent(src, mkt)
    if case == "empty_minutes":
        mkt.minutes[("SYMA", D1)] = []
    elif case == "empty_first_quote":
        mkt.quotes[("SYMA", D1)] = []
    elif case == "missing_volume":
        raw = mkt._daily_json("SYMA", D1, "raw")
        raw.pop("v")
        mkt.daily_override[("SYMA", "raw", D1)] = raw
    else:
        mkt.daily_override[("SYMA", "raw", D1)] = None
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D1)
    (parent,) = ctx.state.parents.values()
    assert parent.arms["T"].unknown, case
    entry = parent.session_log[D1.isoformat()]["resubmission"]["T"]
    assert entry["status"] == "unknown" and entry["fills"] == []


def test_f2_explicit_zero_volume_is_halt_review_not_unknown(tmp_path):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    _parent(src, mkt)
    raw = mkt._daily_json("SYMA", D1, "raw")
    raw["v"] = 0
    mkt.daily_override[("SYMA", "raw", D1)] = raw
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D1)
    (parent,) = ctx.state.parents.values()
    assert parent.review_block == "halt_or_stale" and parent.arms["T"].unknown is None


# ---------------------------------------------------------------- F3 atomic latch


def test_f3_loss_latch_is_inside_the_step_event_and_survives_a_crash(
    tmp_path, monkeypatch
):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    _parent(src, mkt, qty=3, filled=2, avg="99.50", notional="300")
    for day in [CAL.offset(D0, k) for k in range(1, 30)]:
        mkt.daily_override[("SYMA", "raw", day)] = bar_json(
            et(day, 0, 0), o=90, h=90, low=90, c=90, v=1_000_000
        )
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)

    real_append = Ledger.append

    def crash_after_step(self, kind, payload, *, now):
        event = real_append(self, kind, payload, now=now)
        if kind == "step":
            raise SystemExit(73)  # process dies right after the step line
        return event

    monkeypatch.setattr(Ledger, "append", crash_after_step)
    with pytest.raises(SystemExit):
        engine.step(ctx, D1)
    monkeypatch.setattr(Ledger, "append", real_append)

    again = context(ledger, src, mkt)
    assert again.state.latch_types() == {"loss"}
    _parent(src, mkt, sid=2, sym="SYMB", day=D1)
    engine.register(again, D1)
    assert again.state.registered[D1.isoformat()]["assigned"] == []


def test_s1_reconciliation_mismatch_blocks_resubmission_in_the_same_step(tmp_path):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    row = _parent(src, mkt)
    _trade_through(mkt, "SYMA", D1)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    row["filled_qty"] = "1.00000000"
    row["avg_fill_price"] = "99.50000000"
    engine.step(ctx, D1)
    (parent,) = ctx.state.parents.values()
    assert parent.arms["T"].shares == 0
    assert (
        parent.session_log[D1.isoformat()]["resubmission"]["T"]["reason"]
        == "operational_stop_this_step"
    )
    assert "reconciliation_mismatch" in {x["reason"] for x in ctx.state.latches}


# ---------------------------------------------------------------- F4 / F5 / F6 report


def test_f4_fee_schedule_must_cover_every_parent_source():
    bad = json.loads(json.dumps(FEE_SCHEDULE))
    bad["by_source"] = {"unrelated_source": bad["by_source"]["toss_live_order_ledger"]}
    with pytest.raises(FeeScheduleError):
        FeeSchedule.parse(bad)
    bad = json.loads(json.dumps(FEE_SCHEDULE))
    del bad["by_source"]["live_order_ledger"]
    with pytest.raises(FeeScheduleError):
        FeeSchedule.parse(bad)


def _mature(tmp_path, *, fee=True, unknown=False):
    ledger = seal(tmp_path, fee=fee)
    src, mkt = FakeSource(), FakeMarket()
    _parent(src, mkt, sid=1, sym="SYMA")
    _parent(src, mkt, sid=2, sym="SYMB", price="49.50")
    mkt.price["SYMB"] = 50.0
    _trade_through(mkt, "SYMA", D1)
    if unknown:
        mkt.unknown.add(("minute", "SYMB", D1))
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, date(2026, 2, 27))
    return ctx


def test_f5_pending_fee_schedule_withholds_every_net_number(tmp_path):
    rep = R.build_report(_mature(tmp_path, fee=False).state)
    m = rep["metrics"]
    assert m["net_tests"] == "WITHHELD_PENDING_FEE_SCHEDULE"
    assert "M2_T" not in m and "M2_C" not in m and m["holm"] is None
    assert set(m["primary"]) == {"delta_M1"}
    assert not any("M2" in k for k in m["sensitivity"])
    assert rep["verdict"] == R.RESERVED


def test_f6_unknown_parent_keeps_fixed_denominator_and_undetermines_primary(tmp_path):
    rep = R.build_report(_mature(tmp_path, unknown=True).state)
    m = rep["metrics"]
    assert m["n_parents"] == 2 and m["unknown_total"] == 1
    assert m["denominator_budget"] == pytest.approx(2 * 99.5 + 2 * 49.5)
    assert (
        m["primary_status"] == "UNDETERMINED_UNKNOWN_PRESENT" and m["primary"] is None
    )
    cc = m["sensitivity"]["complete_case"]
    assert cc["n_parents"] == 1 and cc["denominator_budget"] == pytest.approx(199.0)
    assert rep["verdict"] == R.RESERVED and "unknown_nonzero" in rep["verdict_reasons"]


# ---------------------------------------------------------------- F7 registration evidence


def _norm(row, rungs=None, deviations=frozenset()):
    rungs = [rung_for(row)] if rungs is None else rungs
    o = assemble_orders([row], rungs, [universe_row(row["symbol"])])[0]
    return normalise(o, CAL, deviations=deviations)


def test_f7_acceptance_outside_regular_session_needs_deviation():
    row = toss_row(1, "SYMA", ordered_at=et(D0, 8, 5), price="99.50", qty=2)
    assert _norm(row)[1] == ["accepted_outside_regular_session"]
    assert (
        _norm(row, deviations=frozenset({"D5_accept_outside_regular_session"}))[1] == []
    )
    assert (
        _norm(toss_row(2, "SYMA", ordered_at=et(D0, 9, 30), price="99.50", qty=2))[1]
        == []
    )
    assert _norm(
        toss_row(
            3,
            "SYMA",
            ordered_at=et(D0, 16, 0),
            price="99.50",
            qty=2,
            expired_at=None,
            status="expired",
        )
    )[1]


def test_f7_missing_rung_or_policy_stamp_or_tif_is_excluded():
    row = toss_row(1, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2)
    assert _norm(row, rungs=[])[1] == ["approved_budget_unavailable"]
    cand, reasons = _norm(
        row,
        rungs=[],
        deviations=frozenset(
            {"D6_ledger_budget_basis", "D4_policy_epoch_invariant_basis"}
        ),
    )
    assert reasons == [] and cand.budget_basis == "ledger_price_x_quantity"
    assert _norm(row, rungs=[rung_for(row, policy_version=None)])[1] == [
        "order_policy_stamp_missing"
    ]
    no_tif = dict(row, time_in_force=None)
    assert "day_tif_unproven" in _norm(no_tif)[1]


# ---------------------------------------------------------------- F8 duplicate minutes


def test_f8_duplicate_minute_bars_are_unknown_not_double_capped():
    t = T0 + timedelta(minutes=1)
    bar = parse_bar(bar_json(t, o=99.7, h=99.8, low=99.0, c=99.6, v=100))
    out = simulate_conservative(
        bars=(bar, bar),
        submit_at=T0,
        close_utc=T1,
        limit=Decimal("99.50"),
        remaining=3,
        affordable=lambda _f: 99,
        ask_lookup=lambda a, b: AskEvidence(OK, found=True),
    )
    assert out.status == UNKNOWN and out.fills == []
    raw = bar_json(t, o=99.7, h=99.8, low=99.0, c=99.6, v=100)
    c, _ = _client(
        [_Resp(200, {"bars": {"SYMA": [raw, dict(raw)]}, "next_page_token": None})]
    )
    res = c.minute_bars("SYMA", T0, T1)
    assert res.status == UNKNOWN and res.reason == "duplicate_bar_timestamp"


# ---------------------------------------------------------------- F9 deviations need approval


def test_f9_seal_refuses_unapproved_mandatory_deviations(tmp_path):
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
    }
    with pytest.raises(ManifestError, match="D1_exit_hold_to_d20"):
        build_manifest(**kwargs)
    with pytest.raises(ManifestError, match="D7_in_window_quote_evidence"):
        build_manifest(
            **kwargs,
            approved_deviations={"D1_exit_hold_to_d20", "D2_resubmit_gate_subset"},
            deviation_ref="hk:doc x",
        )
    mandatory = {
        "D1_exit_hold_to_d20",
        "D2_resubmit_gate_subset",
        "D7_in_window_quote_evidence",
    }
    with pytest.raises(ManifestError, match="deviation-ref"):
        build_manifest(**kwargs, approved_deviations=mandatory)
    m = build_manifest(
        **kwargs, approved_deviations=mandatory, deviation_ref="hk:doc x"
    )
    assert m["deviations"]["approved"] == sorted(mandatory)
    assert (
        m["deviations"]["catalog"]["D3_budget_fill_notional_only"]["mandatory"] is False
    )
    with pytest.raises(ManifestError, match="unknown deviation"):
        build_manifest(**kwargs, approved_deviations={"D9_made_up"}, deviation_ref="x")


# ---------------------------------------------------------------- round 2 (verdict-r2, head d0d8ac5)


def test_r2_n1_truncated_prefix_never_proves_a_prevailing_quote():
    lo = T0 - timedelta(minutes=5)
    pre = [
        quote_json(lo + timedelta(seconds=i), bid=99.49, ask=99.50) for i in range(5)
    ]
    end = T0 + timedelta(minutes=1)
    ev = market_data.ask_evidence_from_quotes(pre, T0, end, Decimal("99.50"), False)
    assert ev.status == UNKNOWN and ev.reason == "pagination_cap"
    # D7: pre-window quotes are never evidence, complete stream or not
    reached = [*pre, quote_json(T0 + timedelta(seconds=10), bid=99.60, ask=99.70)]
    ev = market_data.ask_evidence_from_quotes(reached, T0, end, Decimal("99.50"), False)
    assert ev.status == UNKNOWN and ev.reason == "pagination_cap"
    ev = market_data.ask_evidence_from_quotes(pre, T0, end, Decimal("99.50"), True)
    assert ev.status == UNKNOWN and ev.reason == "no_valid_quote_in_window"


def test_r2_n1_client_page_cap_with_only_old_quotes_is_unknown():
    lo = T0 - timedelta(minutes=5)
    pages = [
        _Resp(
            200,
            {
                "quotes": {
                    "SYMA": [
                        quote_json(lo + timedelta(seconds=i), bid=99.49, ask=99.50)
                    ]
                },
                "next_page_token": f"t{i}",
            },
        )
        for i in range(market_data.MAX_PAGES_PER_WINDOW)
    ]
    c, _ = _client(pages)
    ev = c.ask_at_or_below("SYMA", T0, T0 + timedelta(minutes=1), Decimal("99.50"))
    assert ev.status == UNKNOWN


def test_r2_f7_order_time_policy_hash_is_required():
    row = toss_row(1, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2)
    assert _norm(row, rungs=[rung_for(row, content_hash=None)])[1] == [
        "order_policy_stamp_missing"
    ]
    cand, reasons = _norm(row)
    assert reasons == [] and cand.policy_hash_at_order == POLICY["content_hash"]
    cand, reasons = _norm(
        row,
        rungs=[rung_for(row, content_hash=None)],
        deviations=frozenset({"D4_policy_epoch_invariant_basis"}),
    )
    assert reasons == [] and cand.policy_hash_at_order is None


def test_r2_f7_cap_observations_shape_is_strict():
    from research.rob1391_us_resubmit_shadow.parents import _order_policy_stamp

    ok = [{"rung_index": 0, "policy_version": "v", "content_hash": "h"}]
    assert _order_policy_stamp(ok, 0) == ("v", "h")
    assert _order_policy_stamp(json.dumps(ok), 0) == ("v", "h")
    assert _order_policy_stamp(ok, 1) == (None, None)
    assert _order_policy_stamp(ok + ok, 0) == (None, None)  # ambiguous
    assert _order_policy_stamp([{"rung_index": 0, "policy_version": "v"}], 0) == (
        None,
        None,
    )
    assert _order_policy_stamp("not json", 0) == (None, None)


def test_r2_f7_order_hash_differing_from_seal_stops_the_epoch(tmp_path):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    row = toss_row(1, "SYMA", ordered_at=et(D0, 10, 5), price="99.50", qty=2)
    src.order_rows.append(row)
    src.rung_rows.append(rung_for(row, content_hash="ffffffffffff"))
    src.universe_rows.append(universe_row("SYMA"))
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    assert ctx.state.latch_types() == {"policy_change"}
    assert ctx.state.registered[D0.isoformat()]["exclusions"][0]["reasons"] == [
        "policy_differs_at_order"
    ]


# ---------------------------------------------------------------- round 3 (verdict-r3, head 3558a9e)


def test_r3_n3_quote_replaced_exactly_at_window_start_never_prevailed():
    end = T0 + timedelta(minutes=1)
    rows = [
        quote_json(T0 - timedelta(seconds=1), bid=99.49, ask=99.50, nanos=""),
        quote_json(T0, bid=99.99, ask=100.00, nanos=""),
    ]
    ev = market_data.ask_evidence_from_quotes(rows, T0, end, Decimal("99.50"), True)
    assert ev.status == OK and not ev.found
    bar = parse_bar(bar_json(T0, o=99.9, h=100.1, low=99.49, c=100.0, v=100))
    out = simulate_conservative(
        bars=(bar,),
        submit_at=T0,
        close_utc=T1,
        limit=Decimal("99.50"),
        remaining=1,
        affordable=lambda _f: 1,
        ask_lookup=lambda a, b: market_data.ask_evidence_from_quotes(
            rows, a, b, Decimal("99.50"), True
        ),
    )
    assert out.qty == 0


def test_r3_n3_invalid_update_before_window_clears_the_earlier_quote():
    end = T0 + timedelta(minutes=1)
    rows = [
        quote_json(T0 - timedelta(seconds=5), bid=99.49, ask=99.50, nanos=""),
        {
            "t": z_(T0 - timedelta(seconds=2)),
            "ap": 99.40,
            "as": 1,
            "bp": 99.45,
            "bs": 1,
        },  # crossed update
    ]
    ev = market_data.ask_evidence_from_quotes(rows, T0, end, Decimal("99.50"), True)
    assert ev.status == UNKNOWN and ev.reason == "no_valid_quote_in_window"


def z_(ts):
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_r3_n2_duplicate_parent_stops_the_rest_of_the_batch_and_timing_drift_fails_reconciliation(
    tmp_path,
):
    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    row = _parent(src, mkt, qty=3)
    ctx = context(ledger, src, mkt)
    engine.advance(ctx, D0)
    # same source row re-dated to D1 (timing drift; every other field unchanged)
    row["ordered_at"] = et(D1, 10, 5)
    row["expired_at"] = CAL.session(D1).close_utc + timedelta(minutes=1)
    _parent(src, mkt, sid=2, sym="SYMB", day=D1)
    engine.step(ctx, D1)
    assert "reconciliation_mismatch" in {x["reason"] for x in ctx.state.latches}
    engine.register(ctx, D1)
    assert ctx.state.registered[D1.isoformat()]["assigned"] == []


def test_r3_n2_assign_stops_after_the_first_duplicate(tmp_path):
    from research.rob1391_us_resubmit_shadow.parents import build_episodes

    ledger = seal(tmp_path)
    src, mkt = FakeSource(), FakeMarket()
    ctx = context(ledger, src, mkt)
    a = _parent(src, mkt, sid=1, sym="SYMA")
    b = _parent(src, mkt, sid=2, sym="SYMB")
    orders = assemble_orders(src.order_rows, src.rung_rows, src.universe_rows)
    cands = [(normalise(o, CAL)[0], o) for o in orders]
    episodes = build_episodes(orders, CAL)
    eligible = [(c, episodes.episode_of[o.source_ref]) for c, o in cands]
    # the candidate _assign handles first (sorted by expiry, then episode_id)
    first = min(eligible, key=lambda item: (item[0].expiry_at, item[1]))
    # the first candidate is already a registered parent -> duplicate
    from research.rob1391_us_resubmit_shadow.state import ArmBook, ParentState

    ctx.state.parents[first[1]] = ParentState(
        rec={
            "episode_id": first[1],
            "source_ref": first[0].order.source_ref,
            "symbol": "OLD",
            "d0": "2025-10-01",
            "d20": "2025-10-29",
            "budget": "1",
        },
        arms={k: ArmBook() for k in ("C", "T", "T_opt")},
    )
    assigned, log = engine._assign(ctx, D0, eligible, episodes, {})
    assert assigned == []
    assert [e["reasons"] for e in log] == [
        ["duplicate_parent"],
        ["stopped_after_duplicate_parent"],
    ]
    assert {a["symbol"], b["symbol"]} == {"SYMA", "SYMB"}


# ---------------------------------------------------------------- round 4 (verdict-r4, head 080d811) + D7


def _window_case(rows, *, complete=True, low=99.49, volume=100):
    """Evidence and conservative fill for the minute [T0, T0+1m), P=99.50."""

    end = T0 + timedelta(minutes=1)
    ev = market_data.ask_evidence_from_quotes(rows, T0, end, Decimal("99.50"), complete)
    bar = parse_bar(bar_json(T0, o=99.9, h=100.1, low=low, c=100.0, v=volume))
    out = simulate_conservative(
        bars=(bar,),
        submit_at=T0,
        close_utc=T1,
        limit=Decimal("99.50"),
        remaining=1,
        affordable=lambda _f: 1,
        ask_lookup=lambda a, b: market_data.ask_evidence_from_quotes(
            rows, a, b, Decimal("99.50"), complete
        ),
    )
    return ev, out


def _q(ts_text, *, bid, ask, size=1):
    return {"t": ts_text, "ap": ask, "as": size, "bp": bid, "bs": size}


START = "2025-11-18T14:30:00"
END = "2025-11-18T14:31:00"


def test_r4_b1_nanosecond_timestamps_are_exact():
    assert market_data.parse_ts_ns(START + "Z") == market_data.dt_to_ns(T0)
    assert (
        market_data.parse_ts_ns(START + ".000000001Z") == market_data.dt_to_ns(T0) + 1
    )
    assert (
        market_data.parse_ts_ns(START + ".999999999Z")
        == market_data.dt_to_ns(T0) + 999_999_999
    )
    assert market_data.parse_ts_ns(START + ".1234567891Z") is None  # >9 digits: refused
    assert market_data.parse_ts_ns("2025-11-18 15:00:00Z") is None
    assert market_data.parse_ts_ns(START) is None  # no zone
    assert market_data.ns_to_iso(market_data.dt_to_ns(T0) + 1) == START + ".000000001Z"


@pytest.mark.parametrize("frac", ["000000001", "000000999", "000001000"])
def test_r4_b1_quote_just_after_start_is_in_window_evidence(frac):
    ev, out = _window_case([_q(f"{START}.{frac}Z", bid=99.49, ask=99.50)])
    assert ev.status == OK and ev.found and out.qty == 1


def test_r4_b1_submission_one_ns_after_minute_start_excludes_that_minute():
    from research.rob1391_us_resubmit_shadow.fills import eligible_bars

    bar = parse_bar(bar_json(T0, o=99.9, h=100.1, low=99.0, c=100.0, v=100))
    submit = market_data.dt_to_ns(T0) + 1
    assert eligible_bars((bar,), submit, T1) == []
    assert eligible_bars((bar,), market_data.dt_to_ns(T0), T1) == [bar]


def test_r4_b2_invalid_quote_at_start_is_unknown_not_zero():
    rows = [
        _q("2025-11-18T14:29:59Z", bid=99.49, ask=99.50),
        _q(START + "Z", bid=99.45, ask=99.40),  # crossed
    ]
    ev, out = _window_case(rows)
    assert ev.status == UNKNOWN and ev.reason == "no_valid_quote_in_window"
    assert out.status == UNKNOWN and out.fills == []


def test_d7_window_is_start_inclusive_end_exclusive_at_ns_precision():
    # at start: in
    assert _window_case([_q(START + "Z", bid=99.49, ask=99.50)])[1].qty == 1
    # 1 ns before start: out (pre-window, never carried) -> UNKNOWN
    ev, out = _window_case([_q("2025-11-18T14:29:59.999999999Z", bid=99.49, ask=99.50)])
    assert ev.status == UNKNOWN and out.status == UNKNOWN
    # 1 ns before end: in
    assert (
        _window_case([_q("2025-11-18T14:30:59.999999999Z", bid=99.49, ask=99.50)])[
            1
        ].qty
        == 1
    )
    # exactly at end: out -> UNKNOWN
    ev, out = _window_case([_q(END + "Z", bid=99.49, ask=99.50)])
    assert ev.status == UNKNOWN and out.status == UNKNOWN


def test_d7_valid_quotes_all_above_limit_complete_vs_truncated():
    rows = [_q(START + ".5Z", bid=99.60, ask=99.70)]
    ev, out = _window_case(rows, complete=True)
    assert ev.status == OK and not ev.found and out.status == OK and out.qty == 0
    ev, out = _window_case(rows, complete=False)
    assert (
        ev.status == UNKNOWN and ev.reason == "pagination_cap" and out.status == UNKNOWN
    )


def test_d7_only_invalid_quotes_in_window_is_unknown():
    rows = [_q(START + ".1Z", bid=99.0, ask=0), _q(START + ".2Z", bid=99.6, ask=99.5)]
    ev, out = _window_case(rows)
    assert ev.status == UNKNOWN and out.status == UNKNOWN


def test_d7_client_requests_only_the_window():
    c, sess = _client([_Resp(200, {"quotes": {"SYMA": []}, "next_page_token": None})])
    c.ask_at_or_below("SYMA", T0, T0 + timedelta(minutes=1), Decimal("99.50"))
    # the client sends exact ns bounds and no look-back
    assert sess.kwargs == [{"allow_redirects": False}]
    assert sess.params[0]["start"] == "2025-11-18T14:30:00.000000000Z"
    assert sess.params[0]["end"] == "2025-11-18T14:31:00.000000000Z"


def test_d7_is_a_mandatory_sealed_deviation():
    from research.rob1391_us_resubmit_shadow.manifest import (
        DEVIATIONS,
        MANDATORY_DEVIATIONS,
    )

    assert "D7_in_window_quote_evidence" in MANDATORY_DEVIATIONS
    assert DEVIATIONS["D7_in_window_quote_evidence"][0] is True


def _submit_instant(quote):
    """The submission instant the engine uses (exact ns since D7/round 5)."""

    return getattr(quote, "at_ns", None) or quote.at_utc


def test_r4_b1_sub_microsecond_submission_excludes_the_minute_it_started_in():
    """First quote (= virtual submission) 500 ns after the minute starts: that
    minute's bar began before submission and must not be used. Microsecond
    truncation used to put the submission exactly on the bar start."""

    q = market_data.parse_valid_quote(_q(START + ".000000500Z", bid=99.49, ask=99.50))
    bar = parse_bar(bar_json(T0, o=99.9, h=100.1, low=99.40, c=100.0, v=1_000))
    rows = [_q(START + ".000000600Z", bid=99.49, ask=99.50)]
    out = simulate_conservative(
        bars=(bar,),
        submit_at=_submit_instant(q),
        close_utc=T1,
        limit=Decimal("99.50"),
        remaining=1,
        affordable=lambda _f: 1,
        ask_lookup=lambda a, b: market_data.ask_evidence_from_quotes(
            rows, a, b, Decimal("99.50"), True
        ),
    )
    assert out.qty == 0 and out.candidate_minutes == 0


def test_r4_b1_tester_data_under_d7_is_zero_by_rule():
    """verdict-r4 R4-B1 data: prior 99.50 ask, replaced by 100.00 at start+1ns.
    Under D7 the pre-window quote is not carried, so the only in-window valid
    ask is 100.00 > P: no fill (the operator-approved narrower rule)."""

    rows = [
        _q("2025-11-18T14:29:59Z", bid=99.49, ask=99.50),
        _q(START + ".000000001Z", bid=99.99, ask=100.00),
    ]
    ev, out = _window_case(rows)
    assert ev.status == OK and not ev.found and out.qty == 0
