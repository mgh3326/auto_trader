"""retro_funnel scope fields: the broker and date a stage reports must be the ones
its statements filter on (kr/us/crypto fixtures), the contract must accept them
and reject values outside its closed enums, and removing them must leave the
response byte-identical to the pre-scope golden."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from app.services.retro_v1 import read_retro_funnel
from app.services.retro_v1.funnel_scope import (
    BROKER_ORDER,
    DATE_COLUMNS,
    MARKET_BROKERS,
)
from tests.services.retro_v1.funnel_scope_fixtures import (
    DAY,
    MARKET_SESSIONS,
    RecordingSession,
)
from tests.services.retro_v1.support import SPEC, validate

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "golden" / "retro_funnel_pre_scope.json"
SCOPE_KEYS = ("account_mode", "brokers", "date_basis")

#: Literal bound by the statement -> broker it stands for (for the test's own
#: reading of the executed SQL; the reader uses chain's constants).
_BROKER_BY_LITERAL = {
    "kis_live": "kis",
    "toss_live": "toss",
    "upbit": "upbit",
    "upbit_live": "upbit",
}


async def _funnel(market: str) -> tuple[dict[str, Any], RecordingSession]:
    session = MARKET_SESSIONS[market]()
    response = await read_retro_funnel(session, market=market, from_=DAY, to=DAY)
    return response, session


def _statements(session: RecordingSession, table: str) -> list[tuple[str, dict]]:
    return [(sql, params) for sql, params in session.executed if f"FROM {table}" in sql]


def _where(sql: str) -> str:
    """The WHERE predicate of one compiled statement (before ORDER BY / LIMIT).

    Compiled SQL puts each clause on its own line, so match on whitespace.
    """
    parts = re.split(r"\sWHERE\s", sql, maxsplit=1)
    if len(parts) < 2:
        return ""
    return re.split(r"\s(?:ORDER BY|LIMIT)\s", parts[1], maxsplit=1)[0]


def _bound_values(value: Any) -> list[Any]:
    """Scalars bound by a statement, flattening ``IN (...)`` expanding binds."""
    if isinstance(value, list | tuple | set | frozenset):
        return [v for item in value for v in _bound_values(item)]
    return [value]


def _literals(session: RecordingSession, table: str) -> set[str]:
    found: set[str] = set()
    for _, params in _statements(session, table):
        for value in params.values():
            found.update(
                v
                for v in _bound_values(value)
                if isinstance(v, str) and v in _BROKER_BY_LITERAL
            )
    return found


def _expected_brokers(literals: set[str], market: str) -> list[str]:
    read = {_BROKER_BY_LITERAL[v] for v in literals}
    return [b for b in BROKER_ORDER if b in read and b in MARKET_BROKERS[market]]


# --- contract -------------------------------------------------------------


def _contract_enum(field: str) -> set[str]:
    row = SPEC["components"]["schemas"]["FunnelRow"]["properties"][field]
    if "items" in row:
        return set(row["items"]["enum"])
    return set(row["enum"])


def test_contract_enums_match_the_code_vocabulary() -> None:
    assert _contract_enum("date_basis") == set(DATE_COLUMNS)
    assert _contract_enum("brokers") == set(BROKER_ORDER)
    row = SPEC["components"]["schemas"]["FunnelRow"]
    assert set(row["required"]).isdisjoint(SCOPE_KEYS), "scope fields stay optional"


@pytest.mark.asyncio
async def test_contract_accepts_scope_and_rejects_values_outside_enums() -> None:
    response, _ = await _funnel("kr")
    validate(response, "RetroFunnelResponse")
    assert any("brokers" in row for row in response["data"])

    def rejects(mutate) -> None:
        bad = json.loads(json.dumps(response))
        mutate(bad["data"])
        with pytest.raises(jsonschema.ValidationError):
            validate(bad, "RetroFunnelResponse")

    rejects(lambda rows: rows[-2].__setitem__("date_basis", "order_sent_kst"))
    rejects(lambda rows: rows[-2].__setitem__("account_mode", "kis_mock"))
    rejects(lambda rows: rows[-2].__setitem__("brokers", ["bank"]))
    rejects(lambda rows: rows[-2].__setitem__("brokers", ["kis", "kis"]))


# --- scope agrees with the executed statements ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
async def test_broker_and_date_basis_match_the_statements_that_ran(market: str) -> None:
    response, session = await _funnel(market)
    validate(response, "RetroFunnelResponse")
    rows = {row["stage"]: row for row in response["data"]}

    # Proposal and rung populations are the live proposal account modes.
    proposal_brokers = _expected_brokers(
        _literals(session, "review.order_proposals"), market
    )
    assert rows["proposal"]["brokers"] == proposal_brokers
    assert rows["rung"]["brokers"] == proposal_brokers
    # Orders read three ledgers; their literals are the kis_live / toss_live /
    # live-scope filters bound in those statements.
    order_literals = (
        _literals(session, "review.kis_live_order_ledger")
        | _literals(session, "review.live_order_ledger")
        | _literals(session, "review.toss_live_order_ledger")
    )
    assert rows["order"]["brokers"] == _expected_brokers(order_literals, market)
    # The fill query has no broker filter, so every broker that trades the market.
    assert rows["fill"]["brokers"] == _expected_brokers(set(_BROKER_BY_LITERAL), market)
    for stage, row in rows.items():
        assert row["date_basis"] in DATE_COLUMNS, stage
        for table, column in DATE_COLUMNS[row["date_basis"]]:
            stmts = _statements(session, table)
            assert stmts, (stage, table)
            # The window is a WHERE predicate; the column in SELECT or ORDER BY
            # alone does not filter the population.
            assert any(f"{table}.{column}" in _where(sql) for sql, _ in stmts), (
                stage,
                column,
            )


@pytest.mark.asyncio
async def test_expected_broker_sets_per_market() -> None:
    kr, _ = await _funnel("kr")
    us, _ = await _funnel("us")
    crypto, _ = await _funnel("crypto")

    def brokers(response: dict[str, Any], stage: str) -> list[str]:
        return next(r for r in response["data"] if r["stage"] == stage)["brokers"]

    assert brokers(kr, "order") == ["kis", "toss"]
    assert brokers(us, "order") == ["kis", "toss"]
    assert brokers(crypto, "order") == ["upbit"]
    assert brokers(kr, "fill") == ["kis", "toss"]
    assert brokers(crypto, "proposal") == ["upbit"]


@pytest.mark.asyncio
async def test_populations_outside_accounts_carry_only_the_date_basis() -> None:
    response, _ = await _funnel("kr")
    rows = {row["stage"]: row for row in response["data"]}
    for stage in ("candidate", "rejected_forecast"):
        assert "account_mode" not in rows[stage]
        assert "brokers" not in rows[stage]
        assert rows[stage]["date_basis"] in DATE_COLUMNS
    for stage in ("proposal", "rung", "order", "fill"):
        assert rows[stage]["account_mode"] == "live"
        assert rows[stage]["brokers"]


# --- byte-identical without the new fields --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
async def test_removing_scope_fields_leaves_the_pre_scope_golden_bytes(
    market: str,
) -> None:
    response, _ = await _funnel(market)
    for row in response["data"]:
        for key in SCOPE_KEYS:
            row.pop(key, None)
    response.pop("as_of")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[market]
    rendered = json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True)
    expected = json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=True)
    assert rendered == expected
