"""Behaviour of the plan-only ranking and budget comparison (hk 6790)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.daily_plan.allocation import build_comparison
from app.services.daily_plan.bundle import canonical_json
from app.services.daily_plan.vocabulary import (
    ALLOCATION_REASONS,
    ALLOCATION_STATUSES,
    ENTRY_STATES,
    INELIGIBLE_REASONS,
    INPUT_UNKNOWN_REASONS,
    PARKING_PRECONDITION,
    PARKING_SALE_REASONS,
    PARKING_UNUSABLE_REASONS,
)

KST = timezone(timedelta(hours=9))
CUTOFF = datetime(2026, 11, 26, 8, 30, tzinfo=KST)
STALE_BEFORE = datetime(2026, 11, 25, 15, 30, tzinfo=KST)
SESSION_CLOSE = datetime(2026, 11, 26, 15, 30, tzinfo=KST)
PROVEN = {
    "event_at": "2026-11-25T15:30:00+09:00",
    "first_observed_at": "2026-11-26T08:00:00+09:00",
}
AFTER_CUTOFF = {
    "event_at": "2026-11-26T08:31:00+09:00",
    "first_observed_at": "2026-11-26T08:31:00+09:00",
}
ACCOUNT = "acct_" + "a" * 32
SECOND = "acct_" + "b" * 32
SYMBOLS = [f"{index:06d}" for index in range(1, 13)]
ADDED = ("foreign_net", "news", "disclosures")


def num(value):
    return {"value": value, **PROVEN}


def items(*directions, **times):
    return {
        "items": [
            {"direction": direction, **PROVEN, **times} for direction in directions
        ]
    }


def a_inputs(strength):
    """Higher strength is better on every price, liquidity and book input."""
    return {
        "dist_to_support_pct": num(str(40 - strength)),
        "rsi_14": num(str(80 - strength)),
        "daily_turnover": num(str(1000 + strength)),
        "pbr": num(f"{20 - strength}.5"),
    }


def evidence(symbol, ranking_inputs, last_close="100000"):
    return {
        "row_kind": "candidate",
        "market": "kr",
        "symbol": symbol,
        "account_ref": None,
        "last_close": last_close,
        "sources": {"price": dict(PROVEN)},
        "ranking_inputs": ranking_inputs,
    }


def row(**overrides):
    return {
        "action": "new",
        "disposition": "adopted",
        "valid_until": SESSION_CLOSE.isoformat(),
        "max_buy_price": "100000",
        "would_be_notional": "1000000",
        "would_be_account_ref": ACCOUNT,
        "not_executable_reason": None,
        **overrides,
    }


def account(ref=ACCOUNT, cash="1000000", parking=None):
    return {
        "account_ref": ref,
        "currency": "KRW",
        "cash": num(cash),
        "parking": parking or [],
    }


def parking(symbol="459580", quantity="25", price="105000", cost="1.5", **extra):
    return {
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "sell_cost_bps": cost,
        **PROVEN,
        **extra,
    }


def compare(evidence_rows, candidates, accounts=None, max_adopt=10):
    return build_comparison(
        bundle={"rows": evidence_rows},
        bundle_sha256="f" * 64,
        candidates=candidates,
        account_facts=accounts,
        max_adopt=max_adopt,
        cutoff_at=CUTOFF,
        stale_before=STALE_BEFORE,
        evaluated_at=CUTOFF,
    )


def by_symbol(items_):
    return {item["symbol"]: item for item in items_}


def ranks(block, ranking):
    return {
        item["symbol"]: (
            item[ranking]["rank"],
            item[ranking]["score"],
            item[ranking]["inputs_used"],
        )
        for item in block["candidates"]
    }


def twelve(extra):
    return [
        evidence(symbol, {**a_inputs(12 - index), **extra.get(symbol, {})})
        for index, symbol in enumerate(SYMBOLS)
    ]


ADDED_INPUTS = {
    # The weakest A candidate carries strong proven evidence.
    "000012": {
        "foreign_net": num("900000"),
        "news": items("positive"),
        "disclosures": items("positive"),
    },
    "000001": {
        "foreign_net": num("-900000"),
        "news": items("negative"),
        "disclosures": items("negative"),
    },
    "000002": {"foreign_net": num("0")},
    # Bad news published after the cutoff.
    "000003": {"news": items("negative", **AFTER_CUTOFF)},
}


def test_rankings_differ_only_through_the_added_inputs():
    candidates = {symbol: row() for symbol in SYMBOLS}
    plain = compare(twelve({}), candidates)
    full = compare(twelve(ADDED_INPUTS), candidates)

    assert [ranks(plain, "A")[symbol][0] for symbol in SYMBOLS] == list(range(1, 13))
    assert ranks(full, "A") == ranks(plain, "A")
    assert ranks(plain, "B") == ranks(plain, "A")

    assert ranks(full, "B") != ranks(full, "A")
    first_a = ranks(full, "A")
    first_b = ranks(full, "B")
    assert first_b["000012"][0] < first_a["000012"][0]
    assert first_b["000001"][0] > first_a["000001"][0]
    views = by_symbol(full["candidates"])
    assert views["000012"]["B"]["inputs_used"] == [
        "dist_to_support_pct",
        "rsi_14",
        "daily_turnover",
        "pbr",
        *ADDED,
    ]
    assert all(
        "foreign_net" not in views[symbol]["A"]["inputs_used"] for symbol in SYMBOLS
    )
    for name in ADDED:
        assert views["000005"]["inputs"][name] == {
            "state": "unknown",
            "reason": "input_missing",
            "value": None,
            **({} if name == "foreign_net" else {"used_items": 0, "excluded": {}}),
        }
    # A candidate with no added input keeps exactly its A score.
    assert views["000005"]["B"]["score"] == views["000005"]["A"]["score"]
    assert views["000002"]["inputs"]["foreign_net"] == {
        "state": "present",
        "reason": None,
        "value": "0",
    }


def test_news_after_the_cutoff_is_unknown_in_ranking_b():
    candidates = {symbol: row() for symbol in SYMBOLS}
    with_late_news = compare(twelve(ADDED_INPUTS), candidates)
    without_it = compare(
        twelve({k: v for k, v in ADDED_INPUTS.items() if k != "000003"}), candidates
    )
    late = by_symbol(with_late_news["candidates"])["000003"]
    assert late["inputs"]["news"] == {
        "state": "unknown",
        "reason": "observed_after_cutoff",
        "value": None,
        "used_items": 0,
        "excluded": {"observed_after_cutoff": 1},
    }
    assert "news" not in late["B"]["inputs_used"]
    assert ranks(with_late_news, "B") == ranks(without_it, "B")


@pytest.mark.parametrize(
    ("name", "record", "reason"),
    [
        ("pbr", None, "input_missing"),
        ("pbr", {**PROVEN}, "input_missing"),
        ("pbr", {"unavailable": "price_stale"}, "input_stale"),
        ("pbr", {"unavailable": "something else"}, "input_missing"),
        ("pbr", {"value": "1"}, "observed_time_unproven"),
        (
            "pbr",
            {
                "value": "1",
                **PROVEN,
                "first_observed_at": "2026-11-26T08:00:00",
            },
            "observed_time_unproven",
        ),
        ("pbr", {"value": "1", **AFTER_CUTOFF}, "observed_after_cutoff"),
        (
            "pbr",
            {"value": "1", **PROVEN, "first_observed_at": "2026-11-26T09:00:00+09:00"},
            "observed_after_cutoff",
        ),
        (
            "pbr",
            {"value": "1", **PROVEN, "event_at": "2026-11-24T15:30:00+09:00"},
            "input_stale",
        ),
        ("pbr", {"value": "-1", **PROVEN}, "input_invalid"),
        ("pbr", {"value": 1.5, **PROVEN}, "input_invalid"),
        ("rsi_14", {"value": "101", **PROVEN}, "input_invalid"),
        ("news", None, "input_missing"),
        ("news", {"items": []}, "input_missing"),
        ("news", {"unavailable": "input_stale"}, "input_stale"),
        ("news", {"items": "none"}, "input_invalid"),
        ("news", {"items": [{"direction": "positive"}]}, "observed_time_unproven"),
        ("news", {"items": [{**PROVEN}]}, "direction_unclassified"),
        (
            "news",
            # One proven good item cannot hide an item that might be the bad one.
            {"items": [{"direction": "positive", **PROVEN}, {**PROVEN}]},
            "direction_unclassified",
        ),
        (
            "disclosures",
            {
                "items": [
                    {"direction": "positive", **PROVEN},
                    {"direction": "negative"},
                ]
            },
            "observed_time_unproven",
        ),
    ],
)
def test_unproven_input_is_unknown_and_never_scored(name, record, reason):
    rows_ = [
        evidence("000001", {name: record} if record is not None else {}),
        evidence("000002", {name: record} if record is not None else {}),
    ]
    block = compare(rows_, {"000001": row(), "000002": row()})
    view = block["candidates"][0]
    assert reason in INPUT_UNKNOWN_REASONS
    assert view["inputs"][name]["state"] == "unknown"
    assert view["inputs"][name]["reason"] == reason
    assert view["inputs"][name]["value"] is None
    assert view["B"] == {"rank": None, "score": None, "tied": False, "inputs_used": []}


@pytest.mark.parametrize("name", ["news", "disclosures"])
def test_one_proven_direction_moves_ranking_b_on_its_own_scale(name):
    symbols = ("000001", "000002", "000003")
    extra = {"000001": {name: items("negative")}, "000003": {name: items("positive")}}
    rows_ = [
        evidence(symbol, {**a_inputs(5), **extra.get(symbol, {})}) for symbol in symbols
    ]
    block = compare(rows_, {symbol: row() for symbol in symbols})
    assert [item["A"]["rank"] for item in block["candidates"]] == [1, 1, 1]
    # negative scores 0, no input leaves the 0.5 mean, positive scores 1.
    assert [
        (item["B"]["rank"], item["B"]["score"]) for item in block["candidates"]
    ] == [
        (3, "0.4"),
        (2, "0.5"),
        (1, "0.6"),
    ]
    alone = compare(rows_[:1], {"000001": row()})
    assert alone["candidates"][0]["A"]["rank"] is None
    assert alone["candidates"][0]["B"] == {
        "rank": 1,
        "score": "0",
        "tied": False,
        "inputs_used": [name],
    }


@pytest.mark.parametrize(
    ("directions", "value"),
    [
        (("positive", "positive", "negative"), "1"),
        (("positive", "negative"), "0"),
        (("neutral",), "0"),
        (("negative", "neutral"), "-1"),
    ],
)
def test_proven_items_net_their_direction_and_leave_late_ones_out(directions, value):
    record = items(*directions)
    record["items"].append({"direction": "negative", **AFTER_CUTOFF})
    block = compare([evidence("000001", {"news": record})], {"000001": row()})
    assert block["candidates"][0]["inputs"]["news"] == {
        "state": "present",
        "reason": None,
        "value": value,
        "used_items": len(directions),
        "excluded": {"observed_after_cutoff": 1},
    }


def allocations(block, ranking, policy):
    return by_symbol(block["budget"][ranking][policy]["allocations"])


def waiting_fixture(max_adopt=10, accounts=None):
    rows_ = [
        # Best ranked, but trading above its ceiling.
        evidence("000001", a_inputs(12), last_close="100100"),
        evidence("000002", a_inputs(11)),
        evidence("000003", a_inputs(10)),
    ]
    candidates = {
        "000001": row(),
        "000002": row(),
        "000003": row(would_be_account_ref=SECOND),
    }
    return compare(
        rows_,
        candidates,
        accounts or [account(), account(SECOND, cash="400000")],
        max_adopt,
    )


def assert_totals_stay_inside_funding(block):
    cash = {
        item["account_ref"]: Decimal(item["cash"] or "0")
        for item in block["account_facts"]
    }
    held = {
        (item["account_ref"], holding["symbol"]): Decimal(holding["whole_quantity"])
        for item in block["account_facts"]
        for holding in item["parking"]
    }
    for view in block["candidates"]:
        assert view["entry"]["state"] in ENTRY_STATES
        assert view["ineligible_reason"] in (None, *INELIGIBLE_REASONS)
    for ranking in ("A", "B"):
        for policy in ("W", "N"):
            result = block["budget"][ranking][policy]
            assumed = dict.fromkeys(cash, Decimal(0))
            units = dict.fromkeys(held, Decimal(0))
            for item in result["allocations"]:
                assert item["status"] in ALLOCATION_STATUSES
                assert item["reason"] is None or item["reason"] in ALLOCATION_REASONS
                sold = sum(
                    (Decimal(s["est_proceeds"]) for s in item["would_sell_parking"]),
                    Decimal(0),
                )
                assert sold == Decimal(item["parking_proceeds"])
                assert Decimal(item["allocated"]) == sold + Decimal(item["cash_used"])
                assert Decimal(item["allocated"]) <= Decimal(item["requested"])
                assumed[item["account_ref"]] += sold
                for sale in item["would_sell_parking"]:
                    assert sale["reason"] in PARKING_SALE_REASONS
                    quantity = Decimal(sale["quantity"])
                    assert quantity == int(quantity) and quantity > 0
                    units[item["account_ref"], sale["symbol"]] += quantity
            assert all(units[key] <= held[key] for key in held)
            for totals in result["accounts"]:
                ref = totals["account_ref"]
                assert Decimal(totals["cash_used"]) <= cash[ref]
                assert Decimal(totals["parking_proceeds"]) == assumed[ref]
                assert Decimal(totals["allocated"]) <= cash[ref] + assumed[ref]


def test_waiting_top_candidate_reserves_in_w_and_passes_on_in_n():
    block = waiting_fixture()
    assert by_symbol(block["candidates"])["000001"]["entry"]["state"] == "not_met"
    for ranking in ("A", "B"):
        waits = allocations(block, ranking, "W")
        assert waits["000001"]["status"] == "reserved_waiting"
        assert waits["000001"]["reason"] == "entry_not_met"
        assert waits["000001"]["allocated"] == "1000000"
        assert waits["000002"]["status"] == "not_funded"
        assert waits["000002"]["reason"] == "budget_exhausted"
        assert waits["000002"]["allocated"] == "0"

        moves = allocations(block, ranking, "N")
        assert moves["000001"]["status"] == "skipped_waiting"
        assert moves["000001"]["allocated"] == "0"
        assert moves["000002"]["status"] == "funded"
        assert moves["000002"]["allocated"] == "1000000"
        # The second account is a separate budget.
        for result in (waits, moves):
            assert result["000003"]["status"] == "partially_funded"
            assert result["000003"]["allocated"] == "400000"
    assert_totals_stay_inside_funding(block)


def test_reservation_holds_a_max_adopt_slot():
    block = waiting_fixture(max_adopt=1)
    waits = allocations(block, "A", "W")
    assert waits["000001"]["status"] == "reserved_waiting"
    assert {waits[s]["reason"] for s in ("000002", "000003")} == {"max_adopt_reached"}
    moves = allocations(block, "A", "N")
    assert moves["000002"]["status"] == "funded"
    assert moves["000003"]["reason"] == "max_adopt_reached"
    assert block["caps"] == {"max_adopt": 1}


@pytest.mark.parametrize("max_adopt", [1, 3, 20])
@pytest.mark.parametrize("cash", ["0", "999999.99", "7000000"])
def test_no_policy_ever_assumes_more_than_cash_plus_parking(cash, max_adopt):
    rows_ = twelve(ADDED_INPUTS)
    for index, facts in enumerate(rows_):
        facts["last_close"] = "100100" if index % 3 == 0 else "100000"
    candidates = {
        symbol: row(
            would_be_notional=str(700_000 + index * 333_333),
            would_be_account_ref=ACCOUNT if index % 2 else SECOND,
        )
        for index, symbol in enumerate(SYMBOLS)
    }
    holdings = [
        parking(quantity="7", price="105000.5"),
        parking(symbol="357870", quantity="11.7", price="57005", cost="47.4"),
    ]
    block = compare(
        rows_,
        candidates,
        [account(cash=cash, parking=holdings), account(SECOND, cash=cash)],
        max_adopt,
    )
    assert_totals_stay_inside_funding(block)
    for ranking in ("A", "B"):
        for policy in ("W", "N"):
            taken = [
                item
                for item in block["budget"][ranking][policy]["allocations"]
                if item["allocated"] != "0"
            ]
            assert len(taken) <= max_adopt


def test_short_cash_records_whole_parking_units_and_their_cost():
    rows_ = [evidence("000001", a_inputs(12)), evidence("000002", a_inputs(11))]
    candidates = {
        "000001": row(would_be_notional="3000000"),
        "000002": row(would_be_notional="1000000"),
    }
    block = compare(rows_, candidates, [account(parking=[parking(quantity="25.9")])])
    assert block["account_facts"][0]["parking"][0]["whole_quantity"] == "25"
    for policy in ("W", "N"):
        result = allocations(block, "A", policy)
        assert result["000001"]["status"] == "partially_funded"
        assert result["000001"]["cash_used"] == "1000000"
        # 2,000,000 short / 104,984.25 net per unit = 19.05, rounded down.
        assert result["000001"]["would_sell_parking"] == [
            {
                "symbol": "459580",
                "quantity": "19",
                "est_gross": "1995000",
                "est_cost": "299.25",
                "est_proceeds": "1994700.75",
                "reason": "cash_short",
            }
        ]
        assert result["000001"]["allocated"] == "2994700.75"
        assert result["000001"]["precondition"] == PARKING_PRECONDITION
        # Only the six units left of the 25 held can be assumed next.
        assert result["000002"]["would_sell_parking"][0]["quantity"] == "6"
        assert result["000002"]["allocated"] == "629905.5"
    assert_totals_stay_inside_funding(block)


def test_sufficient_cash_records_no_parking_sale():
    block = compare(
        [evidence("000001", a_inputs(12)), evidence("000002", a_inputs(11))],
        {"000001": row(), "000002": row()},
        [account(cash="2000000", parking=[parking()])],
    )
    for policy in ("W", "N"):
        for result in allocations(block, "A", policy).values():
            assert result["status"] == "funded"
            assert result["would_sell_parking"] == []
            assert result["parking_proceeds"] == "0"
            assert result["precondition"] is None


def test_shortfall_below_one_parking_unit_stays_unfunded():
    block = compare(
        [evidence("000001", a_inputs(12)), evidence("000002", a_inputs(11))],
        {"000001": row(), "000002": row()},
        [account(cash="0", parking=[parking(price="2000000")])],
    )
    result = allocations(block, "A", "N")["000001"]
    assert result["status"] == "not_funded"
    assert result["reason"] == "budget_exhausted"
    assert result["would_sell_parking"] == []


def test_expired_candidate_receives_no_budget_in_either_policy():
    rows_ = [
        evidence("000001", a_inputs(12), last_close="100100"),
        evidence("000002", a_inputs(11)),
    ]
    candidates = {
        # Top ranked and waiting: it would reserve in W if it were live.
        "000001": row(valid_until=CUTOFF.isoformat()),
        "000002": row(),
    }
    block = compare(rows_, candidates, [account()])
    expired = by_symbol(block["candidates"])["000001"]
    assert expired["ineligible_reason"] == "expired"
    assert expired["A"]["rank"] == 1
    for ranking in ("A", "B"):
        for policy in ("W", "N"):
            result = allocations(block, ranking, policy)
            assert "000001" not in result
            assert result["000002"]["status"] == "funded"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"valid_until": None}, "valid_until_unknown"),
        ({"disposition": "dropped"}, "disposition_dropped"),
        ({"action": "hold"}, "no_buy_intent"),
        ({"not_executable_reason": "input_missing"}, "not_executable"),
        ({"would_be_account_ref": None}, "account_unassigned"),
        ({"would_be_account_ref": SECOND}, "account_facts_missing"),
        ({"would_be_notional": None}, "amount_unknown"),
        (
            {
                "would_be_notional": None,
                "would_be_quantity": "1",
                "max_buy_price": None,
                "planned_amount": "5000000",
            },
            "amount_unknown",
        ),
    ],
)
def test_ineligible_rows_never_draw_budget(overrides, reason):
    block = compare(
        [evidence("000001", a_inputs(12))],
        {"000001": row(**overrides)},
        [account()],
    )
    assert reason in INELIGIBLE_REASONS
    assert block["candidates"][0]["ineligible_reason"] == reason
    assert block["budget"]["A"]["W"]["allocations"] == []
    assert block["budget"]["A"]["N"]["allocations"] == []


@pytest.mark.parametrize(
    ("overrides", "requested"),
    [
        (
            {
                "would_be_notional": None,
                "would_be_quantity": "3",
                "would_be_limit_price": "99900",
            },
            "299700",
        ),
        ({"would_be_notional": None, "would_be_quantity": "3"}, "300000"),
        ({"would_be_notional": "7", "planned_amount": "9000000"}, "7"),
        ({"would_be_notional": None, "planned_amount": "5000000"}, "5000000"),
    ],
)
def test_requested_amount_is_the_order_size_before_the_target_total(
    overrides, requested
):
    block = compare(
        [evidence("000001", a_inputs(12))],
        {"000001": row(**overrides)},
        [account()],
    )
    assert block["candidates"][0]["amount"] == requested


@pytest.mark.parametrize(
    ("overrides", "sources", "state"),
    [
        ({}, {"price": PROVEN}, "met"),
        ({"max_buy_price": "99999"}, {"price": PROVEN}, "not_met"),
        (
            {"trigger": {"kind": "price_at_or_below", "value": "99999"}},
            {"price": PROVEN},
            "not_met",
        ),
        (
            {"trigger": {"kind": "price_at_or_above", "value": "100000"}},
            {"price": PROVEN},
            "met",
        ),
        (
            {"trigger": {"kind": "event", "value": "earnings"}},
            {"price": PROVEN},
            "unknown",
        ),
        ({}, {"price": AFTER_CUTOFF}, "unknown"),
        ({}, {}, "unknown"),
        ({"max_buy_price": None}, {"price": PROVEN}, "unknown"),
    ],
)
def test_entry_is_met_only_on_a_proven_price(overrides, sources, state):
    facts = {**evidence("000001", a_inputs(12)), "sources": sources}
    rows_ = [facts, evidence("000002", a_inputs(11))]
    block = compare(rows_, {"000001": row(**overrides), "000002": row()}, [account()])
    assert block["candidates"][0]["entry"]["state"] == state
    top = allocations(block, "A", "N")["000001"]
    assert (top["status"] == "funded") is (state == "met")


@pytest.mark.parametrize(
    ("facts", "cash_reason"),
    [
        ([{**account(), "cash": {"unavailable": "input_missing"}}], "input_missing"),
        ([{**account(), "cash": {"value": "1000000"}}], "observed_time_unproven"),
        (
            [{**account(), "cash": {"value": "1000000", **AFTER_CUTOFF}}],
            "observed_after_cutoff",
        ),
        ([{**account(), "cash": num("-1")}], "input_invalid"),
        ([account(), account()], "input_invalid"),
    ],
)
def test_unknown_cash_funds_nothing_and_assumes_no_sale(facts, cash_reason):
    facts[0]["parking"] = [parking()]
    block = compare(
        [evidence("000001", a_inputs(12)), evidence("000002", a_inputs(11))],
        {"000001": row(), "000002": row()},
        facts,
    )
    assert block["account_facts"][0]["cash"] is None
    assert block["account_facts"][0]["cash_reason"] == cash_reason
    result = allocations(block, "A", "N")["000001"]
    assert result["reason"] == "cash_unknown"
    assert result["would_sell_parking"] == []


@pytest.mark.parametrize(
    ("holdings", "reason"),
    [
        ([parking(symbol="005930")], "not_parking_instrument"),
        ([parking(), parking()], "duplicate_parking_symbol"),
        ([parking(cost=None)], "input_missing"),
        ([parking(price="0")], "input_invalid"),
        ([parking(cost="10000")], "input_invalid"),
        ([parking(quantity="-1")], "input_invalid"),
        (
            [parking(event_at="2026-11-24T15:30:00+09:00")],
            "input_stale",
        ),
        ([parking(first_observed_at=None)], "observed_time_unproven"),
    ],
)
def test_unusable_parking_is_reported_and_never_sold(holdings, reason):
    block = compare(
        [evidence("000001", a_inputs(12)), evidence("000002", a_inputs(11))],
        {"000001": row(), "000002": row()},
        [account(cash="0", parking=holdings)],
    )
    assert reason in PARKING_UNUSABLE_REASONS
    facts = block["account_facts"][0]
    assert facts["parking"] == []
    assert {item["reason"] for item in facts["parking_unusable"]} == {reason}
    assert allocations(block, "A", "N")["000001"]["would_sell_parking"] == []


def test_equal_scores_share_a_rank_and_are_walked_by_symbol():
    rows_ = [evidence(symbol, a_inputs(5)) for symbol in ("000002", "000001")]
    block = compare(rows_, {"000002": row(), "000001": row()}, [account()])
    assert [item["A"] for item in block["candidates"]] == [
        {
            "rank": 1,
            "score": "0.5",
            "tied": True,
            "inputs_used": [
                "dist_to_support_pct",
                "rsi_14",
                "daily_turnover",
                "pbr",
            ],
        }
    ] * 2
    assert [item["symbol"] for item in block["budget"]["A"]["N"]["allocations"]] == [
        "000001",
        "000002",
    ]


def test_unranked_candidate_draws_nothing():
    block = compare([], {"000001": row()}, [account()])
    result = allocations(block, "B", "W")["000001"]
    assert result["status"] == "not_funded"
    assert result["reason"] == "unranked"


def test_block_is_float_free_and_independent_of_input_order():
    candidates = {symbol: row() for symbol in SYMBOLS}
    facts = [account(parking=[parking(), parking(symbol="357870")]), account(SECOND)]
    block = compare(twelve(ADDED_INPUTS), candidates, facts)
    again = compare(
        list(reversed(twelve(ADDED_INPUTS))),
        dict(reversed(candidates.items())),
        list(reversed(facts)),
    )
    # canonical_json refuses floats and non-JSON values.
    assert canonical_json(block) == canonical_json(again)


def test_missing_bundle_leaves_every_input_unknown():
    block = build_comparison(
        bundle=None,
        bundle_sha256=None,
        candidates={"000001": row()},
        account_facts=None,
        max_adopt=10,
        cutoff_at=CUTOFF,
        stale_before=STALE_BEFORE,
        evaluated_at=CUTOFF,
    )
    [view] = block["candidates"]
    assert {item["reason"] for item in view["inputs"].values()} == {"input_missing"}
    assert view["A"]["rank"] is None and view["B"]["rank"] is None
    assert view["ineligible_reason"] == "account_facts_missing"
    assert block["account_facts"] == []
    assert block["input_bundle_sha256"] is None
