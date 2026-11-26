"""The closed candidate rule: ranks, caps, exclusions and source gaps."""

from dataclasses import replace
from decimal import Decimal

from app.services.daily_plan import bundle_builder as builder
from tests.services.daily_plan import bundle_fixtures as f

# The rule itself is stored and hashed; changing it must be visible.
RULE_SHA256 = "7a7f69c01bc6a74d0d49ae78df2bbcdeda42ae72e47e2262c82b924c8124a4c2"


def select(**changes):
    values = {
        "screener_partition": [],
        "screener_latest_date": f.PREVIOUS,
        "picks": [],
        "accounts": [],
    }
    values.update(changes)
    return builder.select_candidates(
        builder.CandidateSources(**values),
        facts=f.facts(),
        closed_at=f.CLOSED_AT,
        bindings=f.BINDINGS,
    )


def symbols_in(selection, source):
    return [s for s, sources in selection.symbols.items() if source in sources]


def screener_rows(count):
    return [
        f.screener(
            index,
            f"{index:06d}",
            str(1000 - index),
            change_rate=Decimal(index),
            consecutive_up_days=index % 6,
            dist_to_support_pct=Decimal(index) if index % 2 else None,
        )
        for index in range(1, count + 1)
    ]


def test_each_screener_rank_is_capped_in_its_own_order():
    selection = select(screener_partition=screener_rows(100))
    assert symbols_in(selection, "screener:turnover_leaders") == [
        f"{i:06d}" for i in range(1, 41)
    ]
    assert symbols_in(selection, "screener:gainers") == [
        f"{i:06d}" for i in range(81, 101)
    ]
    streak = symbols_in(selection, "screener:consecutive_up")
    assert len(streak) == 20 and all(int(s) % 6 >= 3 for s in streak)
    assert symbols_in(selection, "screener:support_proximity") == [
        f"{i:06d}" for i in range(1, 40, 2)
    ]


def test_excluded_symbols_do_not_use_up_a_cap():
    rows = screener_rows(41)
    rows[0] = replace(rows[0], symbol="459580")
    rows[1] = replace(rows[1], symbol="0000D0")
    selection = select(screener_partition=rows)
    assert selection.excluded == {
        "non_six_digit_symbol": 1,
        "parking_instrument": 1,
    }
    assert not {"459580", "0000D0"} & set(selection.symbols)
    assert len(symbols_in(selection, "screener:turnover_leaders")) == 39


def pick(row_id, symbol, source="screen_stocks", **changes):
    row = builder.PickRow(
        row_id=row_id,
        call_id=f"call-{row_id}",
        source=source,
        symbol=symbol,
        selection_seq=row_id,
        recorded_at=f.at(f.PREVIOUS, 10),
    )
    return replace(row, **changes)


def test_pick_log_caps_per_source_and_in_total():
    rows = [
        pick(source * 100 + index, f"{source * 100 + index:06d}", f"preset-{source}")
        for source in range(1, 6)
        for index in range(30)
    ]
    selection = select(picks=rows)
    per_source = [
        len(symbols_in(selection, f"pick_log:preset-{n}")) for n in range(1, 6)
    ]
    assert per_source == [20, 20, 20, 0, 0]
    assert selection.sources["pick_log"]["symbols"] == 60


def test_pick_log_keeps_every_call_of_a_chosen_symbol():
    rows = [pick(1, f.SYMBOL), pick(2, f.SYMBOL, recorded_at=f.at(f.PREVIOUS, 11))]
    selection = select(picks=rows)
    assert selection.call_ids[f.SYMBOL] == ("call-1", "call-2")
    assert selection.sources["pick_log"]["symbols"] == 1


def test_picks_after_the_cutoff_or_before_the_window_are_not_candidates():
    selection = select(picks=[pick(1, f.SYMBOL, recorded_at=f.LATE)])
    assert not selection.symbols
    assert selection.sources["pick_log"]["reason"] == "observed_after_cutoff"
    old = pick(1, f.SYMBOL, recorded_at=f.at(f.FLOW_SESSIONS[0], 10))
    selection = select(picks=[old])
    assert selection.sources["pick_log"]["reason"] == "input_missing"


def test_holdings_are_never_capped_and_name_their_accounts():
    held = [
        {"symbol": f"{index:06d}", "quantity": "1", "currency": "KRW"}
        for index in range(1, 201)
    ]
    held.append({"symbol": "999999", "quantity": "0", "currency": "KRW"})
    rows = [
        f.account_row(holdings=held),
        f.account_row(42, f.TOSS, None, holdings=held[:1]),
    ]
    selection = select(accounts=rows)
    assert len(selection.symbols) == 200
    assert selection.holders["000001"] == (f.KIS_REF, f.TOSS_REF)
    assert selection.holders["000002"] == (f.KIS_REF,)
    assert selection.sources["holdings"]["status"] == "ok"


def test_upbit_accounts_are_not_kr_holdings():
    selection = select(accounts=[f.account_row(account="upbit:upbit:" + "d" * 64)])
    assert not selection.symbols
    assert selection.sources["holdings"]["reason"] == "input_missing"


def test_snapshot_written_after_the_cutoff_does_not_supply_holdings():
    late = f.account_row(observed_at=f.LATE, created_at=f.LATE)
    selection = select(accounts=[late])
    assert not selection.symbols
    assert selection.sources["holdings"]["reason"] == "observed_after_cutoff"


def test_screener_gap_is_reported_instead_of_zero_candidates():
    late = f.screener(1, f.SYMBOL, "1", updated_at=f.LATE)
    older = f.sessions(2)[0]
    for changes, reason in (
        ({"screener_partition": [late]}, "observed_after_cutoff"),
        ({"screener_latest_date": older}, "input_stale"),
        ({"screener_latest_date": None}, "input_missing"),
    ):
        selection = select(**changes)
        assert not selection.symbols
        assert selection.sources["screener"]["status"] == "unavailable"
        assert selection.sources["screener"]["reason"] == reason
    body = f.build(candidate_sources=builder.CandidateSources([], None, [], []))
    assert body["gap_reason"] == "candidate_source_unavailable"
    assert body["rows"] == []


def test_rule_and_candidate_set_are_hashed():
    body = f.build()
    assert body["candidate_rule"] == builder.CANDIDATE_RULE
    assert body["candidate_rule_sha256"] == RULE_SHA256
    fewer = f.build(candidate_sources=replace(f.sources(), picks=[]))
    assert fewer["candidate_set_sha256"] != body["candidate_set_sha256"]
    assert fewer["candidate_rule_sha256"] == body["candidate_rule_sha256"]
