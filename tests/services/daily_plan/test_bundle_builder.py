"""The premarket bundle: stable bytes, honest unknowns, keys the comparison reads."""

import json
from dataclasses import replace
from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from app.mcp_server.tooling import analysis_quick
from app.services.daily_candles.repository import DailyCandleRow
from app.services.daily_plan import bundle_builder as builder
from app.services.daily_plan.allocation import build_comparison
from app.services.daily_plan.bundle import bundle_sha256, canonical_bundle
from app.services.daily_plan.validator import prepare_row
from tests.services.daily_plan import bundle_fixtures as f

INPUTS = (
    "dist_to_support_pct",
    "rsi_14",
    "daily_turnover",
    "pbr",
    "foreign_net",
    "news",
    "disclosures",
)
ITEM_INPUTS = ("news", "disclosures")
CANDLE_INPUTS = ("dist_to_support_pct", "rsi_14")
# The fixture's canonical bytes. A change here is a change of the stored format
# or of a computed value; say so in the change that moves it.
GOLDEN_SHA256 = "5c26ecd30481a512923cffe1171b0e7a8d829a36eb298909ab3ce4f617719f2a"


def comparison(body, rows=None, max_adopt=10):
    return build_comparison(
        bundle=body,
        bundle_sha256=bundle_sha256(body),
        candidates=rows or {row["symbol"]: {} for row in body["rows"]},
        account_facts=body["accounts"],
        max_adopt=max_adopt,
        cutoff_at=f.CLOSED_AT,
        stale_before=f.facts().stale_before,
        evaluated_at=f.CLOSED_AT,
    )


def reading(body, name, symbol=f.SYMBOL):
    view = next(
        item for item in comparison(body)["candidates"] if item["symbol"] == symbol
    )
    return view["inputs"][name]


def test_same_rows_give_the_same_bytes_and_golden_hash():
    first, second = f.build(), f.build()
    assert canonical_bundle(first) == canonical_bundle(second)
    assert bundle_sha256(first) == GOLDEN_SHA256
    # The stored form is the canonical form, so it re-serializes to itself.
    assert (
        json.loads(canonical_bundle(first).decode()) == first
        and first["bundle_closed_at"] == "2026-11-26T23:20:00.000000Z"
    )


def test_row_order_from_the_database_does_not_change_the_bytes():
    rows = f.sources()
    reordered = builder.CandidateSources(
        screener_partition=rows.screener_partition[::-1],
        screener_latest_date=rows.screener_latest_date,
        picks=rows.picks[::-1],
        accounts=list(rows.accounts)[::-1],
    )
    data = f.inputs()
    shuffled = replace(
        data,
        bars=data.bars[::-1],
        flows=data.flows[::-1],
        news=(f.news(25), f.news(21)),
    )
    straight = replace(data, news=(f.news(21), f.news(25)))
    assert canonical_bundle(
        f.build({f.SYMBOL: shuffled}, candidate_sources=reordered)
    ) == canonical_bundle(f.build({f.SYMBOL: straight}))


def test_bundle_holds_no_float_and_every_input_key():
    body = f.build()

    def walk(value):
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body)
    empty = f.build({f.SYMBOL: builder.SymbolInputs()})
    record = f.evidence(empty)
    assert set(record["ranking_inputs"]) == set(INPUTS)
    assert all(
        value["unavailable"] == "input_missing"
        for value in record["ranking_inputs"].values()
    )
    assert record["last_close"] == {"unavailable": "input_missing"}
    assert record["sources"]["price"]["unavailable"] == "input_missing"


def test_fixture_values_are_present_and_directions_are_absent():
    body = f.build()
    for name in ("dist_to_support_pct", "rsi_14", "daily_turnover", "pbr"):
        assert reading(body, name)["state"] == "present", name
    assert reading(body, "foreign_net")["value"] == "2500"
    assert f.evidence(body)["ranking_inputs"]["foreign_net"]["unit"] == "shares"
    for name in ITEM_INPUTS:
        items = f.evidence(body)["ranking_inputs"][name]["items"]
        assert items and all("direction" not in item for item in items)
        assert reading(body, name)["reason"] == "direction_unclassified"


def test_rsi_and_support_match_the_quick_analysis_projection():
    data = f.inputs()
    rows = [
        DailyCandleRow(
            time_utc=f.at(bar.session, 9).astimezone(UTC),
            symbol=f.SYMBOL,
            partition="KRX",
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            adj_close=None,
            volume=float(bar.volume),
            value=0.0,
            source=bar.source,
        )
        for bar in data.bars
    ]
    quick = analysis_quick._project_symbol(f.SYMBOL, "equity_kr", rows)
    record = f.evidence(f.build())["ranking_inputs"]
    assert Decimal(record["rsi_14"]["value"]) == Decimal(str(quick["rsi_14"]))
    close = data.bars[-1].close
    nearest = Decimal(str(quick["supports"][0]["price"]))
    assert Decimal(record["dist_to_support_pct"]["value"]) == (
        (close - nearest) / close * 100
    ).quantize(Decimal("0.0001"))


def _missing(data, name):
    if name in CANDLE_INPUTS:
        return replace(data, bars=())
    if name == "daily_turnover":
        return replace(data, screener=None)
    if name == "pbr":
        return replace(data, valuation=None)
    field = {"foreign_net": "flows"}.get(name, name)
    return replace(data, **{field: ()})


def _updated_after_create(data, name):
    later = f.at(f.DAY, 7, 0)
    if name in CANDLE_INPUTS:
        # Candle rows have no created_at: an update only moves ``ingested_at``.
        last = replace(data.bars[-1], ingested_at=f.LATE)
        return replace(data, bars=(*data.bars[:-1], last))
    if name == "daily_turnover":
        return replace(data, screener=replace(data.screener, updated_at=later))
    if name == "pbr":
        return replace(data, valuation=replace(data.valuation, updated_at=later))
    if name == "foreign_net":
        rows = list(data.flows)
        rows[1] = replace(rows[1], updated_at=later)
        return replace(data, flows=rows)
    if name == "news":
        return replace(data, news=(replace(data.news[0], revised_at=later),))
    row = replace(data.disclosures[0], updated_at=later)
    return replace(data, disclosures=(row,))


def _observed_after_cutoff(data, name):
    late = f.LATE
    if name in CANDLE_INPUTS:
        return replace(
            data, bars=tuple(replace(bar, ingested_at=late) for bar in data.bars)
        )
    if name in ("daily_turnover", "pbr"):
        field = "screener" if name == "daily_turnover" else "valuation"
        row = replace(getattr(data, field), created_at=late, updated_at=late)
        return replace(data, **{field: row})
    if name == "foreign_net":
        rows = list(data.flows)
        rows[-1] = replace(rows[-1], created_at=late, updated_at=late)
        return replace(data, flows=rows)
    if name == "news":
        row = replace(data.news[0], link_first_seen_at=late, first_received_at=late)
        return replace(data, news=(row,))
    row = replace(
        data.disclosures[0],
        created_at=late,
        updated_at=late,
        first_received_at=late,
    )
    return replace(data, disclosures=(row,))


@pytest.mark.parametrize("name", INPUTS)
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_missing, "input_missing"),
        (_updated_after_create, "observed_time_unproven"),
        (_observed_after_cutoff, "observed_after_cutoff"),
    ],
    ids=["missing", "updated_after_create", "observed_after_cutoff"],
)
def test_each_unusable_input_has_its_closed_reason(name, change, reason):
    if change is _updated_after_create and name in CANDLE_INPUTS:
        reason = "observed_after_cutoff"
    body = f.build({f.SYMBOL: change(f.inputs(), name)})
    record = f.evidence(body)["ranking_inputs"][name]
    if "items" in record:
        # The item stays, with the unproven time spelled out.
        [item] = record["items"]
        assert item["first_observed_at"] == {"unavailable": reason}
    else:
        assert record["unavailable"] == reason
        assert "value" not in record
    view = reading(body, name)
    assert (view["state"], view["reason"]) == ("unknown", reason)
    # Only the changed source is affected.
    untouched = f.evidence(f.build())["ranking_inputs"]
    for other in set(INPUTS) - {name} - set(CANDLE_INPUTS):
        assert f.evidence(body)["ranking_inputs"][other] == untouched[other]
    # The other candidate keeps its own inputs.
    assert f.evidence(body, f.OTHER) == f.evidence(f.build(), f.OTHER)


def test_candle_rewrite_before_the_cutoff_stays_present():
    data = f.inputs()
    rewritten = tuple(replace(bar, ingested_at=f.at(f.DAY, 8, 19)) for bar in data.bars)
    body = f.build({f.SYMBOL: replace(data, bars=rewritten)})
    assert f.evidence(body)["ranking_inputs"]["rsi_14"]["first_observed_at"] == (
        "2026-11-26T23:19:00.000000Z"
    )


def test_one_old_bar_rewritten_late_only_hides_the_windows_that_use_it():
    data = f.inputs()
    old = (replace(data.bars[0], ingested_at=f.LATE), *data.bars[1:])
    record = f.evidence(f.build({f.SYMBOL: replace(data, bars=old)}))
    assert record["ranking_inputs"]["rsi_14"]["unavailable"] == (
        "observed_after_cutoff"
    )
    assert record["ranking_inputs"]["dist_to_support_pct"]["value"]
    assert record["last_close"] == f.evidence(f.build())["last_close"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda data: replace(data, bars=data.bars[:-1]),
            "input_stale",
        ),
        (
            lambda data: replace(
                data,
                bars=(
                    *data.bars[:-1],
                    replace(data.bars[-1], ingested_at=f.at(f.PREVIOUS, 11)),
                ),
            ),
            "input_invalid",
        ),
        (
            lambda data: replace(
                data,
                bars=(
                    *data.bars[:-3],
                    *(replace(bar, volume=Decimal(0)) for bar in data.bars[-3:]),
                ),
            ),
            "input_invalid",
        ),
    ],
    ids=["newest_bar_missing", "forming_bar", "halted_suspect"],
)
def test_unusable_price_is_unknown_for_every_candle_input(change, reason):
    record = f.evidence(f.build({f.SYMBOL: change(f.inputs())}))
    assert record["last_close"] == {"unavailable": reason}
    for name in CANDLE_INPUTS:
        assert record["ranking_inputs"][name]["unavailable"] == reason


def test_stale_snapshot_and_missing_values_are_not_zero():
    data = f.inputs()
    older = f.sessions(2)[0]
    cases = {
        "input_stale": replace(
            data,
            valuation=replace(
                data.valuation,
                snapshot_date=older,
                created_at=f.at(older, 16, 30),
                updated_at=f.at(older, 16, 30),
            ),
        ),
        "input_missing": replace(data, valuation=replace(data.valuation, pbr=None)),
        "input_invalid": replace(
            data, valuation=replace(data.valuation, pbr=Decimal("-1"))
        ),
    }
    for reason, changed in cases.items():
        record = f.evidence(f.build({f.SYMBOL: changed}))["ranking_inputs"]["pbr"]
        assert record["unavailable"] == reason


def test_flow_sum_needs_every_confirmed_session():
    data = f.inputs()
    for rows, reason in (
        (data.flows[:-1], "input_stale"),
        ([*data.flows[:2], *data.flows[3:]], "input_missing"),
        ([replace(data.flows[0], foreign_net=None), *data.flows[1:]], "input_missing"),
    ):
        body = f.build({f.SYMBOL: replace(data, flows=rows)})
        assert reading(body, "foreign_net")["reason"] == reason
    zero = [replace(row, foreign_net=0) for row in data.flows]
    body = f.build({f.SYMBOL: replace(data, flows=zero)})
    assert reading(body, "foreign_net") == {
        "state": "present",
        "reason": None,
        "value": "0",
    }


def test_items_published_before_the_window_are_stale_not_present():
    data = f.inputs()
    old = replace(data.news[0], published_at=f.at(f.PREVIOUS, 9, 0))
    body = f.build({f.SYMBOL: replace(data, news=(old,))})
    assert reading(body, "news")["reason"] == "input_stale"


def test_relevance_exclusion_counts_only_when_judged_by_the_cutoff():
    data = f.inputs()
    excluded = replace(data.news[0], status="excluded", judged_at=f.at(f.DAY, 6, 0))
    body = f.build({f.SYMBOL: replace(data, news=(excluded,))})
    assert reading(body, "news")["reason"] == "input_missing"
    later = replace(excluded, judged_at=f.LATE)
    body = f.build({f.SYMBOL: replace(data, news=(later,))})
    assert reading(body, "news")["reason"] == "direction_unclassified"


@pytest.mark.parametrize(
    ("changes", "event_at"),
    [
        (
            {"provider_created_at": None, "receipt_text": "2026-11-26T17:00:00"},
            "2026-11-26T08:00:00.000000Z",
        ),
        (
            {"provider_created_at": None, "receipt_text": "20261126"},
            {"unavailable": "observed_time_unproven"},
        ),
    ],
    ids=["dart_receipt_clock", "dart_date_only"],
)
def test_disclosure_time_comes_only_from_a_recorded_clock(changes, event_at):
    data = f.inputs()
    row = replace(data.disclosures[0], source="dart", **changes)
    record = f.evidence(f.build({f.SYMBOL: replace(data, disclosures=(row,))}))
    [item] = record["ranking_inputs"]["disclosures"]["items"]
    assert item["event_at"] == event_at


def cash_of(rows):
    body = f.build(account_rows=rows)
    [entry] = [a for a in body["accounts"] if a["account_ref"] == f.KIS_REF]
    [view] = [
        a for a in comparison(body)["account_facts"] if a["account_ref"] == f.KIS_REF
    ]
    return body, entry["cash"], view


def test_cash_read_zero_is_known_and_unreadable_cash_is_unavailable():
    zero_body, zero, zero_view = cash_of([f.account_row(cash="0E-8")])
    none_body, none, none_view = cash_of([f.account_row(cash=None)])
    assert zero["value"] == "0" and "unavailable" not in zero
    assert (zero_view["cash"], zero_view["cash_reason"]) == ("0", None)
    assert none == {
        "unavailable": "input_missing",
        "source_name": builder.ACCOUNT_SOURCE,
    }
    assert (none_view["cash"], none_view["cash_reason"]) == (None, "input_missing")
    assert canonical_bundle(zero_body) != canonical_bundle(none_body)
    assert bundle_sha256(zero_body) != bundle_sha256(none_body)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "input_missing"),
        ([f.account_row(cash="-1")], "input_invalid"),
        ([f.account_row(cash="n/a")], "input_invalid"),
        ([f.account_row(reasons=["duplicate_cash"])], "input_invalid"),
        (
            [f.account_row(observed_at=f.LATE, created_at=f.LATE)],
            "observed_after_cutoff",
        ),
        (
            [
                f.account_row(
                    day=f.PREVIOUS,
                    observed_at=f.at(f.PREVIOUS, 7, 40),
                    created_at=f.at(f.PREVIOUS, 7, 41),
                )
            ],
            "input_stale",
        ),
    ],
)
def test_unusable_cash_reasons(rows, reason):
    _, cash, view = cash_of(rows)
    assert cash["unavailable"] == reason
    assert view["cash_reason"] == reason


def test_parking_is_listed_but_unusable_without_a_sell_cost():
    body = f.build()
    entry = next(a for a in body["accounts"] if a["account_ref"] == f.KIS_REF)
    [holding] = entry["parking"]
    assert (holding["symbol"], holding["quantity"], holding["price"]) == (
        "459580",
        "3",
        "1000000",
    )
    assert holding["sell_cost_bps"] == {"unavailable": "input_missing"}
    view = next(
        a for a in comparison(body)["account_facts"] if a["account_ref"] == f.KIS_REF
    )
    assert view["parking"] == []
    assert view["parking_unusable"] == [{"symbol": "459580", "reason": "input_missing"}]


def test_unbound_account_series_is_reported_and_gets_no_reference():
    stranger = f.account_row(43, "kis:kis_live:" + "c" * 64)
    body = f.build(account_rows=[*f.accounts(), stranger])
    assert body["accounts_unidentified"] == [
        {"broker": "kis", "reason": "account_unidentified"}
    ]
    assert {a["account_ref"] for a in body["accounts"]} == {f.KIS_REF, f.TOSS_REF}
    assert "c" * 64 not in canonical_bundle(body).decode()


def plan_row(symbol, price):
    return {
        "row_kind": "candidate",
        "market": "kr",
        "symbol": symbol,
        "account_ref": None,
        "action": "new",
        "action_basis": "scored",
        "venue": "KRX",
        "session": "regular",
        "side": "buy",
        "thesis": "fixture",
        "disposition": "adopted",
        "would_be_account_ref": f.KIS_REF,
        "max_buy_price": price,
        "would_be_limit_price": price,
        "would_be_notional": "300000",
        "valid_until": "2026-11-27T15:30:00+09:00",
    }


def test_comparison_reads_the_bundle_and_funds_both_policies():
    body = f.build()
    rows = {symbol: plan_row(symbol, "500000") for symbol in (f.SYMBOL, f.OTHER)}
    result = comparison(body, rows)
    assert any(
        view["state"] == "present"
        for candidate in result["candidates"]
        for view in candidate["inputs"].values()
    )
    for ranking in ("A", "B"):
        assert result["candidates"][0][ranking]["rank"] is not None
        for policy in ("W", "N"):
            allocations = result["budget"][ranking][policy]["allocations"]
            assert [item["status"] for item in allocations] == ["funded", "funded"]
    assert all(c["entry"]["state"] == "met" for c in result["candidates"])


def test_publish_validator_accepts_the_bundle_evidence():
    body = f.build()
    facts = f.facts()
    row = prepare_row(
        plan_row(f.SYMBOL, "70000"),
        evidence=f.evidence(body),
        asof=f.CLOSED_AT + timedelta(minutes=1),
        session_close=f.at(f.DAY, 15, 30),
        bundle_closed_at=f.CLOSED_AT,
        oldest_accepted_at=facts.stale_before,
        no_add_floor_pct=Decimal(-8),
    )
    assert row["not_executable_reason"] is None
    assert row["would_be_account_mode"] == "kis_live"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: replace(d, bars=d.bars[:-1]), "input_stale"),
        (lambda d: replace(d, bars=()), "input_missing"),
        (
            lambda d: replace(
                d, bars=tuple(replace(b, ingested_at=f.LATE) for b in d.bars)
            ),
            "observed_time_unproven",
        ),
    ],
    ids=["stale", "missing", "after_cutoff"],
)
def test_publish_validator_downgrades_an_unusable_price(change, reason):
    body = f.build({f.SYMBOL: change(f.inputs())})
    facts = f.facts()
    row = prepare_row(
        plan_row(f.SYMBOL, "70000"),
        evidence=f.evidence(body),
        asof=f.CLOSED_AT + timedelta(minutes=1),
        session_close=f.at(f.DAY, 15, 30),
        bundle_closed_at=f.CLOSED_AT,
        oldest_accepted_at=facts.stale_before,
        no_add_floor_pct=Decimal(-8),
    )
    assert row["action"] == "watch"
    assert row["not_executable_reason"] == reason
