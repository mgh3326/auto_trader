"""The writer stores the comparison once and never shows it to a session."""

import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Insert

from app.mcp_server.tooling import daily_plan_registration as registration
from app.models.daily_plan import rows, versions
from app.services.daily_plan import store
from app.services.daily_plan.bundle import (
    SCHEMA_VERSION,
    bundle_sha256,
    canonical_bundle,
)
from app.services.daily_plan.vocabulary import (
    ALLOCATION_METADATA_KEY,
    ALLOCATION_SCHEMA_VERSION,
)

PREVIOUS = date(2026, 11, 25)
DAY = date(2026, 11, 26)
NEXT = date(2026, 11, 27)
NOW = datetime(2026, 11, 26, 1, tzinfo=UTC)
ACCOUNT = "acct_" + "a" * 32
PROVEN = {
    "event_at": "2026-11-25T15:30:00+09:00",
    "first_observed_at": "2026-11-26T08:00:00+09:00",
}


class Values(list):
    def mappings(self):
        return self

    def one_or_none(self):
        return self[0] if self else None

    def all(self):
        return self


class FakeDb:
    """The statements the daily-plan store issues, answered from memory."""

    def __init__(self, bundle):
        self.bundle = bundle
        self.head_id = uuid4()
        self.versions = []
        self.rows = []

    async def scalar(self, stmt):
        sql = str(stmt)
        if "max(" in sql:
            return max((item["seq"] for item in self.versions), default=None)
        if "daily_plan_row_links" in sql:
            return None
        return self.head_id

    async def execute(self, stmt, *_args):
        if isinstance(stmt, Insert):
            body = dict(stmt.compile().params)
            if stmt.table is versions:
                self.versions.append(body)
            elif stmt.table is rows:
                self.rows.append({**body, "execution_state": "plan_only"})
            return Values()
        columns = [column.name for column in stmt.selected_columns]
        params = stmt.compile().params
        if columns[0] == "session_date":
            return Values(
                [
                    {
                        "session_date": DAY,
                        "head_id": self.head_id,
                        "seq": len(self.versions),
                    }
                ]
            )
        if "version_id" in columns and "metadata" in columns:
            # The fold: real SQL also removes the comparison from metadata.
            metadata = {item["id"]: item["metadata"] for item in self.versions}
            return Values(
                {**row, "metadata": dict(metadata[row["version_id"]])}
                for row in self.rows
            )
        if "version_id" in columns:
            return Values(
                row for row in self.rows if row["version_id"] == params["version_id_1"]
            )
        if "sha256" in columns:
            return Values([self.bundle])
        if "idempotency_key" in columns:
            found = self.versions
            for field in ("idempotency_key", "seq"):
                if f"{field}_1" in params:
                    found = [v for v in found if v[field] == params[f"{field}_1"]]
            if "session_date_1" in params and params["session_date_1"] != DAY:
                found = []
            return Values(copy.deepcopy(found[-1:]))
        return Values()

    @asynccontextmanager
    async def begin(self):
        yield


def _evidence(symbol, strength):
    return {
        "row_kind": "candidate",
        "market": "kr",
        "symbol": symbol,
        "account_ref": None,
        "last_close": "90000",
        "sources": {"price": dict(PROVEN)},
        "routable_account_modes": {ACCOUNT: "kis_live"},
        "ranking_inputs": {"rsi_14": {"value": str(50 - strength), **PROVEN}},
    }


def _bundle():
    body = json.loads(
        canonical_bundle(
            {
                "schema_version": SCHEMA_VERSION,
                "bundle_closed_at": "2026-11-26T08:20:00+09:00",
                "rows": [_evidence("005930", 2), _evidence("278470", 1)],
                "accounts": [
                    {
                        "account_ref": ACCOUNT,
                        "currency": "KRW",
                        "cash": {"value": "500000", **PROVEN},
                        "parking": [],
                    }
                ],
            }
        )
    )
    return {
        "id": uuid4(),
        "sha256": bundle_sha256(body),
        "body": body,
        "created_at": NOW,
    }


def _row(symbol):
    return {
        "row_kind": "candidate",
        "market": "kr",
        "symbol": symbol,
        "venue": "KRX",
        "session": "regular",
        "action": "new",
        "action_basis": "scored",
        "side": "buy",
        "thesis": "Support held",
        "max_buy_price": "90000",
        "would_be_limit_price": "90000",
        "would_be_quantity": "2",
        "would_be_account_ref": ACCOUNT,
    }


def _request(bundle, seq=0, key="first", symbols=("005930", "278470")):
    return {
        "expected_prev_seq": seq,
        "idempotency_key": key,
        "asof": "2026-11-26T08:25:00+09:00",
        "strategy_id": "kr-plan",
        "strategy_rule_version": "v1",
        "kind": "session",
        "input_bundle_id": str(bundle["id"]),
        "input_bundle_sha256": bundle["sha256"],
        "rows": [_row(symbol) for symbol in symbols],
    }


@pytest.fixture
def calendar(monkeypatch):
    def bounds(_, day):
        return (
            datetime(day.year, day.month, day.day, 0, tzinfo=UTC),
            datetime(day.year, day.month, day.day, 6, 30, tzinfo=UTC),
        )

    monkeypatch.setattr(store, "regular_session_bounds", bounds)
    monkeypatch.setattr(store, "previous_trading_session", lambda *_: PREVIOUS)
    monkeypatch.setattr(store, "next_trading_session", lambda *_: NEXT)


async def _write(db, request):
    policy = registration.load_trading_policy()
    return await store.write_version(
        db, day=DAY, request=request, now=NOW, policy=policy, policy_hash="hash"
    )


@pytest.mark.asyncio
async def test_publication_stores_the_comparison_and_replay_keeps_it(
    calendar, monkeypatch
):
    bundle = _bundle()
    db = FakeDb(bundle)
    first = await _write(db, _request(bundle))
    [version] = db.versions
    block = version["metadata"][ALLOCATION_METADATA_KEY]
    assert block["schema_version"] == ALLOCATION_SCHEMA_VERSION
    assert block["input_bundle_sha256"] == bundle["sha256"]
    # The information boundary is the referenced bundle's close.
    assert block["cutoff_at"] == "2026-11-26T08:20:00+09:00"
    assert block["stale_before"] == "2026-11-25T06:30:00+00:00"
    assert block["caps"] == {
        "max_adopt": registration.load_trading_policy().daily_plan.max_adopt
    }
    assert [item["A"]["rank"] for item in block["candidates"]] == [1, 2]
    funded = block["budget"]["B"]["N"]["allocations"]
    assert [(item["symbol"], item["allocated"]) for item in funded] == [
        ("005930", "180000"),
        ("278470", "180000"),
    ]
    stored = copy.deepcopy(version)

    def refuse(**_):
        raise AssertionError("a replay must not recompute the comparison")

    monkeypatch.setattr(store, "build_comparison", refuse)
    replay = await _write(db, _request(bundle))
    assert replay["id"] == first["id"]
    assert db.versions == [stored]
    changed = _request(bundle)
    changed["rows"][0]["thesis"] = "changed"
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await _write(db, changed)
    assert db.versions == [stored]


@pytest.mark.asyncio
async def test_partial_publication_compares_every_candidate_in_force(calendar):
    bundle = _bundle()
    db = FakeDb(bundle)
    await _write(db, _request(bundle))
    await _write(db, _request(bundle, seq=1, key="second", symbols=("278470",)))
    block = db.versions[1]["metadata"][ALLOCATION_METADATA_KEY]
    assert [item["symbol"] for item in block["candidates"]] == ["005930", "278470"]
    # The untouched higher-ranked row still takes its money first.
    assert [item["symbol"] for item in block["budget"]["A"]["W"]["allocations"]] == [
        "005930",
        "278470",
    ]


@pytest.mark.asyncio
async def test_no_mcp_response_carries_the_comparison(calendar, monkeypatch):
    bundle = _bundle()
    db = FakeDb(bundle)

    @asynccontextmanager
    async def session():
        yield db

    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(registration, "AsyncSessionLocal", session)
    monkeypatch.setattr(registration, "datetime", Clock)
    monkeypatch.setattr(registration.settings, "daily_plan_enabled", True)

    request = _request(bundle)
    # A watch row with carry lands in the next session's list queue.
    request["rows"][1].update(
        action="watch",
        side=None,
        trigger={"kind": "price_at_or_below", "value": "90000"},
        invalidation_conditions=[{"kind": "event", "value": "support lost"}],
        carry_allowed=True,
    )
    written = await registration.daily_plan_write_version(DAY, request)
    replayed = await registration.daily_plan_write_version(DAY, request)
    latest = await registration.daily_plan_get(DAY)
    selected = await registration.daily_plan_get(DAY, seq=1)
    confirmed = await registration.daily_plan_confirm_links(DAY, seq=1)
    Clock.current = datetime(2026, 11, 26, 7, tzinfo=UTC)
    monkeypatch.setattr(store, "previous_trading_session", lambda *_: DAY)
    listed = await registration.daily_plan_list(queue_for=NEXT)

    # The comparison exists, so its absence below is the strip at work.
    assert ALLOCATION_METADATA_KEY in db.versions[0]["metadata"]
    for response in (written, replayed, latest, selected):
        assert response["metadata"]["asof"] == request["asof"]
        assert len(response["rows"]) == 2
    assert [row["symbol"] for row in listed["premarket_reevaluation_queue"]] == [
        "278470"
    ]
    assert confirmed["link_complete"] is True
    for response in (written, replayed, latest, selected, confirmed, listed):
        assert "error_code" not in response
        text = json.dumps(response, default=str)
        assert ALLOCATION_METADATA_KEY not in text
        assert ALLOCATION_SCHEMA_VERSION not in text
