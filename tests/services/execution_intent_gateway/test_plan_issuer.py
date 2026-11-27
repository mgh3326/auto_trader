"""hk 6991 — the intent issuer reads only executable rows of PUBLISHED versions.

``read_issuable_row`` is exercised with a fake session (the DB round trip of the
daily_plan tables is covered by the daily_plan DB tests in CI).
"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.services.daily_plan import store as plan_store
from app.services.execution_intent_gateway import plan_binding
from app.services.execution_intent_gateway.plan_issuer import (
    IssueRefused,
    IssueRequest,
    build_intent,
    issue_intent,
)
from tests.services.execution_intent_gateway.support import NOW
from tests.services.execution_intent_gateway.test_plan_version import (
    ACCOUNT,
    KEY,
    SYMBOL,
    _row,
)

ROOT = Path(__file__).resolve().parents[3]
EXECUTABLE = SimpleNamespace(daily_plan=SimpleNamespace(executable_markets={"kr"}))
PLAN_ONLY = SimpleNamespace(daily_plan=SimpleNamespace(executable_markets=frozenset()))


def _request(**overrides: Any) -> IssueRequest:
    values: dict[str, Any] = {
        "market": "kr",
        "day": NOW.date(),
        "seq": 1,
        "row_key": KEY,
        "position": 0,
    }
    return IssueRequest(**{**values, **overrides})


def _version(**overrides: Any) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "head_id": uuid4(),
        "seq": 1,
        "status": "PUBLISHED",
        "strategy_id": "plan-entry-test",
        "policy_version": "policy-v1",
        "metadata": {},
        "mode": "executable",
        **overrides,
    }


class _Result:
    def __init__(self, record: dict | None) -> None:
        self.record = record

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict | None:
        return self.record


class _Db:
    def __init__(self, version: dict | None) -> None:
        self.version = version

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self.version)


class _Writer:
    def __init__(self) -> None:
        self.stored: list[Any] = []

    async def store_intent(self, intent: Any, *, now: Any) -> None:
        self.stored.append(intent)


def _fold_with(state: str, body: dict | None = None):
    body = body or _row()

    async def fold(_db: Any, _head: Any, _seq: int) -> list[dict]:
        return [
            {
                "row_kind": "candidate",
                "market": "kr",
                "symbol": SYMBOL,
                "account_ref": None,
                "action": body["action"],
                "execution_state": state,
                "body": body,
            }
        ]

    return fold


async def _issue(monkeypatch, *, version, state="executable", policy=EXECUTABLE):
    monkeypatch.setattr(plan_store, "_fold", _fold_with(state))
    writer = _Writer()
    intent = await issue_intent(
        _Db(version), writer, request=_request(), policy=policy, now=NOW
    )
    return intent, writer


@pytest.mark.asyncio
async def test_an_executable_row_of_a_published_version_is_issued(monkeypatch) -> None:
    intent, writer = await _issue(monkeypatch, version=_version())
    assert writer.stored == [intent]
    assert intent.schema_version == 3 and intent.account == "kis_mock"
    assert intent.plan.plan_version == 1 and intent.plan.row_key == KEY
    assert intent.plan.account_ref == ACCOUNT and intent.plan.venue == "KRX"
    assert intent.plan.invalidation_price_krw == 65_000
    assert (intent.max_quantity, intent.limit_price_krw) == (10, 70_000)
    assert intent.max_slippage_bps == 71  # 70,500 ceiling over the 70,000 limit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "state", "policy", "code"),
    [
        (_version(), "executable", PLAN_ONLY, "market_not_executable"),
        (None, "executable", EXECUTABLE, "version_not_found"),
        (_version(status="DRAFT"), "executable", EXECUTABLE, "version_not_published"),
        (_version(mode="plan_only"), "executable", EXECUTABLE, "head_not_executable"),
        (_version(), "plan_only", EXECUTABLE, "row_not_executable"),
    ],
)
async def test_the_issuer_refuses_what_is_not_an_executable_published_row(
    monkeypatch, version, state, policy, code
) -> None:
    with pytest.raises(IssueRefused) as refused:
        await _issue(monkeypatch, version=version, state=state, policy=policy)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"action": "watch"}, "row_action_not_issuable"),
        ({"action": "risk_sell"}, "row_action_not_issuable"),
        ({"not_executable_reason": "input_stale"}, "row_blocked"),
        ({"disposition": "dropped"}, "row_blocked"),
        ({"would_be_account_mode": "kis_live"}, "account_not_supported"),
        ({"would_be_account_mode": "upbit_live"}, "account_not_supported"),
        ({"would_be_account_ref": None}, "account_unassigned"),
        ({"venue": "NXT"}, "venue_not_supported"),
        ({"invalidation_conditions": []}, "invalidation_missing"),
        ({"would_be_limit_price": "70000.5"}, "price_not_whole_krw"),
        ({"would_be_quantity": "100"}, "notional_cap_exceeded"),
        ({"valid_until": (NOW - timedelta(minutes=1)).isoformat()}, "row_expired"),
    ],
)
def test_build_refusals(overrides: dict, code: str) -> None:
    body = _row(**overrides)
    with pytest.raises(IssueRefused) as refused:
        build_intent(
            version=_version(),
            row={"body": body, "action": body["action"], "symbol": SYMBOL},
            request=_request(),
            now=NOW,
        )
    assert refused.value.code == code


def test_a_trim_row_is_its_own_sell_intent_without_invalidation() -> None:
    body = _row(
        row_kind="holding",
        account_ref=ACCOUNT,
        would_be_account_ref=None,
        would_be_account_mode=None,
        account_mode="kis_mock",
        action="trim",
        side="sell",
        trigger={"kind": "price_at_or_above", "value": "80000"},
        min_sell_price="80000",
        would_be_quantity="3",
    )
    key = ("holding", "kr", SYMBOL, ACCOUNT)
    intent = build_intent(
        version=_version(),
        row={"body": body, "action": "trim", "symbol": SYMBOL},
        request=_request(row_key=key, position=5),
        now=NOW,
    )
    assert intent.side == "sell" and intent.plan.action == "trim"
    assert intent.plan.invalidation_price_krw is None
    assert (
        intent.inventory.min_position_after,
        intent.inventory.max_position_after,
    ) == (2, 5)
    entry = build_intent(
        version=_version(),
        row={"body": _row(), "action": "new", "symbol": SYMBOL},
        request=_request(),
        now=NOW,
    )
    assert entry.intent_id != intent.intent_id


def test_v3_not_sent_reasons_match_the_trigger() -> None:
    path = ROOT / "alembic/versions/20261127_hk6991_intent_v3.py"
    spec = importlib.util.spec_from_file_location("hk6991_pin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.V3_NOT_SENT_REASONS) == plan_binding.V3_NOT_SENT_REASONS
    guard = module.GUARD_DDL[0]
    assert "intent_version = 3 AND NEW.refusal_reason NOT IN " in guard
    assert "intent_version = 2 AND NEW.refusal_reason NOT IN " in guard


def test_the_migration_is_additive_and_downgrade_refuses_first() -> None:
    path = ROOT / "alembic/versions/20261127_hk6991_intent_v3.py"
    spec = importlib.util.spec_from_file_location("hk6991_shape", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    upgrade = "\n".join((*module.ALTER_DDL, *module.GUARD_DDL, module.GRANT_DDL))
    for word in (
        "DROP COLUMN",
        "DROP TABLE",
        "DELETE FROM",
        "TRUNCATE",
        "GRANT DELETE",
    ):
        assert word not in upgrade, word
    assert "pg_roles WHERE rolname = 'at_app'" in module.GRANT_DDL
    downgrade = path.read_text().split("def downgrade")[1]
    assert downgrade.index("DOWNGRADE_GUARD") < downgrade.index("_DOWNGRADE_DDL")
    assert "schema_version = 3" in module.DOWNGRADE_GUARD


def test_caps_are_unchanged() -> None:
    from app.schemas.execution_intent import MAX_INTENT_NOTIONAL_KRW
    from app.services.kis_mock_runner.envelope import LOCKED_ENVELOPE

    assert MAX_INTENT_NOTIONAL_KRW == 5_000_000
    assert LOCKED_ENVELOPE.max_single_notional_krw == 5_000_000
