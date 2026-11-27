"""Publishing against {"unavailable": reason} bundle values (design §7.1, §7.2).

Every case goes through the real write path with a stored bundle, so a value the
validator or the risk replay cannot read fails the whole publication here.
"""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.models.daily_plan import bundles
from app.services.daily_plan import store
from app.services.daily_plan.bundle import bundle_sha256, canonical_bundle
from app.services.daily_plan.risk import UNAVAILABLE_SKIPS
from app.services.daily_plan.vocabulary import REASON_CODES, UNAVAILABLE_REASONS
from app.services.mandate.account_ref import derive_account_ref
from app.services.trading_policy_service import load_trading_policy, policy_content_hash
from tests.services.daily_plan import bundle_fixtures
from tests.services.daily_plan.test_publish import (
    CLOSE,
    NOW,
    Values,
    fake_db,
    request,
)

REF = derive_account_ref("kis_mock", "test-account", secret="test-only")
SYMBOL = "278470"
PROVEN = {"event_at": NOW.isoformat(), "first_observed_at": NOW.isoformat()}


@pytest.fixture(autouse=True)
def _calendar(monkeypatch):
    monkeypatch.setattr(
        store, "previous_trading_session", lambda *_: date(2026, 11, 25)
    )
    monkeypatch.setattr(
        store,
        "regular_session_bounds",
        lambda _, day: (NOW.replace(day=day.day), CLOSE.replace(day=day.day)),
    )


async def publish_body(body: dict, rows: list[dict], *, now=NOW) -> list[dict]:
    stored = {"sha256": bundle_sha256(body), "body": body, "created_at": now}
    db = fake_db(current=0)
    inner = db.execute.side_effect

    async def execute(stmt, *args):
        if bundles in getattr(stmt, "get_final_froms", list)():
            return Values([stored])
        return await inner(stmt, *args)

    db.execute.side_effect = execute
    result = await store.write_version(
        db,
        day=now.date(),
        request=request(
            asof=now.isoformat(),
            input_bundle_id="00000000-0000-0000-0000-000000000001",
            input_bundle_sha256=stored["sha256"],
            rows=rows,
        ),
        now=now,
        policy=load_trading_policy(),
        policy_hash=policy_content_hash(),
    )
    assert result["status"] == "PUBLISHED"
    return result["rows"]


async def publish(evidence: list[dict], rows: list[dict]) -> list[dict]:
    body = json.loads(
        canonical_bundle(
            {
                "schema_version": "daily-plan-bundle/v1",
                "bundle_closed_at": NOW.isoformat(),
                "rows": evidence,
            }
        )
    )
    return await publish_body(body, rows)


def candidate_evidence(price: dict) -> dict:
    return {
        "row_kind": "candidate",
        "market": "kr",
        "symbol": SYMBOL,
        "account_ref": None,
        "sources": {"price": price},
    }


def holding_evidence(**risk_changes) -> dict:
    return {
        "row_kind": "holding",
        "market": "kr",
        "symbol": SYMBOL,
        "account_ref": REF,
        "account_mode": "kis_live",
        "account_scope": "routable",
        "account_ref_kind": "derived",
        "average_costs": ["100"],
        "last_close": "100",
        "sources": {
            name: dict(PROVEN)
            for name in (
                "price",
                "holdings",
                "position",
                "open_orders",
                "protected_quantity",
            )
        },
        "risk_inputs": {
            "quantity": "10",
            "average_cost": "100",
            "plan": None,
            "bars": [],
            "price_fresh": True,
            "open_sell_quantity": {"known": "0"},
            "protected_quantity": {"known": "0"},
            **risk_changes,
        },
    }


def holding_row(action: str) -> dict:
    row = {
        "row_kind": "holding",
        "market": "kr",
        "symbol": SYMBOL,
        "account_ref": REF,
        "venue": "KRX",
        "session": "regular",
        "action": action,
        "action_basis": "scored",
        "thesis": "Held position",
    }
    if action == "add":
        row.update(
            side="buy",
            max_buy_price="90100",
            would_be_limit_price="90100",
            would_be_quantity="1",
        )
    elif action in ("trim", "risk_sell"):
        row.update(side="sell", min_sell_price="100", would_be_quantity="1")
    return row


def assert_watch(row: dict, reason: str) -> None:
    assert row["action"] == "watch"
    assert row["side"] is None
    assert row["reason_code"] == reason
    assert row["not_executable_reason"] == reason
    assert reason in REASON_CODES


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["event_at", "first_observed_at"])
@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS)
async def test_unavailable_source_time_publishes_watch(field, reason):
    price = {**PROVEN, field: {"unavailable": reason}}
    [row] = await publish([candidate_evidence(price)], request()["rows"])
    assert_watch(row, reason)
    assert row["input_asof"]["price"][field] is None


@pytest.mark.asyncio
async def test_reason_outside_the_closed_list_is_still_unknown():
    price = {**PROVEN, "event_at": {"unavailable": "surprise"}}
    [row] = await publish([candidate_evidence(price)], request()["rows"])
    assert_watch(row, "input_missing")


@pytest.mark.asyncio
async def test_absent_source_outranks_a_stated_reason():
    evidence = holding_evidence()
    del evidence["sources"]["holdings"]
    evidence["sources"]["price"]["event_at"] = {"unavailable": "input_stale"}
    [row] = await publish([evidence], [holding_row("hold")])
    assert_watch(row, "input_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hold", "trim", "add"])
async def test_proven_holding_is_not_downgraded(action):
    """Control: the holding fixture downgrades only for the value a case breaks."""
    [row] = await publish([holding_evidence()], [holding_row(action)])
    assert row["action"] == action
    assert row["not_executable_reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS)
async def test_unavailable_time_on_an_unrequired_source_keeps_the_action(reason):
    evidence = holding_evidence()
    evidence["sources"]["position"] = {
        "event_at": {"unavailable": reason},
        "first_observed_at": {"unavailable": reason},
    }
    [row] = await publish([evidence], [holding_row("hold")])
    assert row["action"] == "hold"
    assert row["not_executable_reason"] is None


PLAN = {
    "stop_price": "90",
    "stop_basis": "support",
    "backfilled": False,
    "id": 1,
    "version": 1,
}
BAR = {"session": "2026-11-25", "high": "101", "low": "99", "close": "100"}
RISK_PATHS = {
    "risk_inputs": ((), "position_unreadable"),
    "quantity": (("quantity",), "position_unreadable"),
    "average_cost": (("average_cost",), "position_unreadable"),
    "plan": (("plan",), "no_exit_plan"),
    "plan.stop_price": (("plan", "stop_price"), "no_exit_plan"),
    "plan.stop_basis": (("plan", "stop_basis"), "no_exit_plan"),
    "bars": (("bars",), "price_missing"),
    "bars.close": (("bars", 0, "close"), "price_missing"),
    "bars.open": (("bars", 0, "open"), "price_missing"),
    "price_fresh": (("price_fresh",), "price_stale"),
    "prior_decision": (("prior_decision",), "position_unreadable"),
}


def break_risk_input(path: tuple, reason: str) -> dict:
    evidence = holding_evidence(plan=deepcopy(PLAN), bars=[dict(BAR)])
    unknown = {"unavailable": reason}
    if not path:
        evidence["risk_inputs"] = unknown
        return evidence
    target = evidence["risk_inputs"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = unknown
    return evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS)
@pytest.mark.parametrize("action", ["hold", "risk_sell"])
@pytest.mark.parametrize("name", RISK_PATHS)
async def test_unavailable_risk_input_publishes_watch(name, action, reason):
    path, default_skip = RISK_PATHS[name]
    [row] = await publish([break_risk_input(path, reason)], [holding_row(action)])
    assert_watch(row, reason)
    verdict = row["rule_verdict"]
    assert verdict["result"] == "skip"
    assert verdict["status"] == "unknown"
    assert verdict["skip_reason"] == (
        reason if reason in UNAVAILABLE_SKIPS else default_skip
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS)
@pytest.mark.parametrize("field", ["open_sell_quantity", "protected_quantity"])
@pytest.mark.parametrize("action", ["add", "trim", "risk_sell"])
async def test_unavailable_order_quantity_publishes_watch(action, field, reason):
    evidence = holding_evidence(**{field: {"unavailable": reason}})
    [row] = await publish([evidence], [holding_row(action)])
    assert_watch(row, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS)
@pytest.mark.parametrize(
    "change",
    [
        lambda value: {"average_costs": value},
        lambda value: {"average_costs": ["100", value]},
        lambda value: {"last_close": value},
    ],
    ids=["average_costs", "one_cost", "last_close"],
)
async def test_unavailable_add_floor_input_publishes_watch(change, reason):
    evidence = {**holding_evidence(), **change({"unavailable": reason})}
    [row] = await publish([evidence], [holding_row("add")])
    assert_watch(row, reason)


@pytest.mark.asyncio
async def test_unavailable_volume_is_left_to_the_loss_cut_rule():
    evidence = holding_evidence(
        plan=dict(PLAN),
        bars=[{**BAR, "volume": {"unavailable": "input_missing"}}],
    )
    [row] = await publish([evidence], [holding_row("hold")])
    assert row["action"] == "hold"
    assert row["not_executable_reason"] is None


@pytest.mark.asyncio
async def test_builder_bundle_publishes_its_unavailable_values(monkeypatch):
    """The assembler's own bytes: late bars and an item time it cannot prove."""
    monkeypatch.setattr(
        store, "previous_trading_session", lambda *_: bundle_fixtures.PREVIOUS
    )
    inputs = bundle_fixtures.inputs()
    body = bundle_fixtures.build(
        {
            bundle_fixtures.SYMBOL: replace(
                inputs,
                bars=[
                    replace(bar, ingested_at=bundle_fixtures.LATE)
                    for bar in inputs.bars
                ],
                news=(replace(inputs.news[0], first_received_at=None),),
            )
        }
    )
    evidence = bundle_fixtures.evidence(body)
    assert evidence["sources"]["price"]["unavailable"] == "observed_after_cutoff"
    [news_item] = evidence["ranking_inputs"]["news"]["items"]
    assert news_item["first_observed_at"] == {"unavailable": "observed_time_unproven"}
    row = {**request()["rows"][0], "symbol": bundle_fixtures.SYMBOL}
    [published] = await publish_body(
        body, [row], now=datetime(2026, 11, 27, 1, tzinfo=UTC)
    )
    # A whole unavailable source keeps its mapping from before this change.
    assert_watch(published, "observed_time_unproven")
