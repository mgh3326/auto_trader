"""hk 5441 — what must stay true about the code and the migration themselves.

No database: the migration is read as text, and import closures are measured in
a fresh interpreter.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from app.models.execution_mandates import (
    ExecutionAccountControl,
    ExecutionChildOrderEvidence,
    ExecutionExitHandoff,
    ExecutionMandate,
    ExecutionMandateRevocation,
    ExecutionOperatorExit,
)
from app.schemas.execution_intent import (
    ExecutionIntentV1,
    ExecutionIntentV2,
    SubmitChild,
    UpbitSubmitChild,
    contract_digest,
    parse_child_request,
)
from app.services.execution_intent_gateway import mandates
from app.services.execution_intent_gateway.decisions import MOCK_ACCOUNTS
from app.services.execution_intent_gateway.upbit_decisions import (
    LEDGER_ACCOUNT_MODE,
    LEDGER_BROKER,
    NOT_SENT_REASONS,
    ORDER_LEDGER_ACCOUNT_SCOPE,
    SLEEVE_ACCOUNT_MODE,
    SLEEVE_MARKET,
    UPBIT_ACCOUNTS,
)
from app.services.strategy_sleeves.vocabulary import ACCOUNT_MARKETS, LEDGER_IDENTITY
from tests.services.execution_intent_gateway.upbit_support import NOW

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261120_hk5441_upbit_intent_gateway.py"
PACKAGE = ROOT / "app/services/execution_intent_gateway"

#: Anything that can reach a broker, an order endpoint or the network.
_FORBIDDEN_FOR_SHADOW = (
    "app.services.brokers",
    "app.mcp_server",
    "app.services.execution_ledger",
    "app.services.order_proposals",
    "app.services.toss",
    "app.core.db",
    "httpx",
    "sqlalchemy",
    "redis",
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("hk5441_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ §8.2-3 shadow module


def test_the_shadow_forwarder_import_closure_holds_no_order_code() -> None:
    probe = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import app.services.execution_intent_gateway.upbit_shadow_forwarder\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    reached = [
        name
        for name in loaded
        if any(
            name == bad or name.startswith(bad + ".") for bad in _FORBIDDEN_FOR_SHADOW
        )
    ]
    assert reached == []
    assert "app.services.execution_intent_gateway.forward_result" in loaded


def test_the_shadow_forwarder_source_imports_only_the_result_type() -> None:
    tree = ast.parse((PACKAGE / "upbit_shadow_forwarder.py").read_text("utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported == {
        "__future__",
        "typing",
        "app.services.execution_intent_gateway.forward_result",
    }


def test_the_live_forwarder_is_the_only_module_that_names_the_order_functions() -> None:
    callers = [
        path.name
        for path in sorted(PACKAGE.glob("upbit_*.py"))
        if "_place_order_impl" in path.read_text("utf-8")
        or "cancel_order_impl" in path.read_text("utf-8")
    ]
    assert callers == ["upbit_live_forwarder.py"]


def test_the_served_gateway_keeps_upbit_out_of_the_mock_forwarders() -> None:
    from app.routers import execution_intent_gateway as route

    gateway = route.get_intent_gateway()
    assert set(gateway._forwarders) == set(MOCK_ACCOUNTS)
    assert gateway._upbit is not None
    assert UPBIT_ACCOUNTS.isdisjoint(MOCK_ACCOUNTS)


# ------------------------------------------------------------ §1.3 one account, three names


def test_the_account_names_agree_with_the_sleeve_vocabulary() -> None:
    assert (SLEEVE_ACCOUNT_MODE, SLEEVE_MARKET) in ACCOUNT_MARKETS
    assert LEDGER_IDENTITY[SLEEVE_ACCOUNT_MODE] == (LEDGER_BROKER, LEDGER_ACCOUNT_MODE)
    assert ORDER_LEDGER_ACCOUNT_SCOPE == "upbit_live"


# ------------------------------------------------------------ D9, D2


def test_d9_ceilings_are_the_decided_values() -> None:
    assert mandates.MAX_PER_INTENT_NOTIONAL_KRW == Decimal("480000")
    assert mandates.MAX_OPEN_INTENTS == 4
    assert mandates.MAX_PRICE_BAND_BPS == 30
    assert mandates.MAX_INTENT_TTL == timedelta(hours=12)
    assert mandates.MAX_MANDATE_LIFETIME == timedelta(days=91)
    assert mandates.MAX_OPERATOR_EXIT_LIFETIME == timedelta(hours=1)


@pytest.mark.parametrize("actor", ["", "  ", "system:sleeve_loss_gate", "SYSTEM:x"])
def test_a_system_or_blank_actor_is_refused(actor: str) -> None:
    with pytest.raises(mandates.MandateError):
        mandates.human_actor(actor)


# ------------------------------------------------------------ migration shape


def test_not_sent_reasons_are_the_same_in_the_trigger_and_in_python() -> None:
    assert set(_migration().NOT_SENT_REASONS) == set(NOT_SENT_REASONS)


def test_the_migration_grants_are_conditional_and_grant_no_delete() -> None:
    grant = _migration().GRANT_DDL
    assert "SELECT 1 FROM pg_roles WHERE rolname = 'at_app'" in grant
    assert "DELETE" not in grant.upper()
    assert "TRUNCATE" not in grant.upper()
    # FOR UPDATE on the mandate row needs UPDATE on one column; the trigger
    # still refuses every real update.
    assert "GRANT UPDATE (id) ON review.execution_mandates" in grant
    assert "GRANT UPDATE (refusal_reason) ON review.execution_child_requests" in grant


def test_the_upgrade_is_additive() -> None:
    module = _migration()
    upgrade = "\n".join((*module.TABLE_DDL, *module.ALTER_DDL)).upper()
    assert "DROP TABLE" not in upgrade and "DROP COLUMN" not in upgrade
    assert "DELETE FROM" not in upgrade and "UPDATE REVIEW" not in upgrade


def test_the_downgrade_refuses_before_it_drops_anything() -> None:
    module = _migration()
    guard = module.DOWNGRADE_GUARD
    for table in (
        "execution_mandates",
        "execution_mandate_revocations",
        "execution_operator_exits",
        "execution_exit_handoffs",
        "execution_child_order_evidence",
    ):
        assert f"EXISTS (SELECT 1 FROM review.{table})" in guard
    assert "schema_version <> 1" in guard and "execution_path <> 'card'" in guard
    assert "state IN ('shadowed', 'not_sent')" in guard
    assert "exit_handoff_id IS NOT NULL" in guard
    source = MIGRATION.read_text("utf-8")
    body = source[source.index("def downgrade()") :]
    assert body.index("DOWNGRADE_GUARD") < body.index("_DOWNGRADE_DDL")


def test_the_account_starts_frozen() -> None:
    [seed] = _migration().SEED_DDL
    assert "'upbit_live', 'GLOBAL_FREEZE'" in seed


def test_every_guarded_table_refuses_delete_and_truncate() -> None:
    guards = "\n".join(_migration().GUARD_DDL)
    for table in (
        "execution_mandates",
        "execution_mandate_revocations",
        "execution_operator_exits",
        "execution_exit_handoffs",
        "execution_child_order_evidence",
        "execution_account_control",
    ):
        assert f"BEFORE TRUNCATE ON review.{table}" in guards, table
    assert "BEFORE INSERT OR UPDATE OR DELETE ON review.execution_mandates" in guards
    assert "BEFORE UPDATE OR DELETE ON review.execution_mandate_revocations" in guards
    assert "BEFORE UPDATE OR DELETE ON review.execution_operator_exits" in guards


def test_the_card_shape_is_checked_at_commit_and_on_a_later_rung() -> None:
    guards = "\n".join(_migration().GUARD_DDL)
    assert "CREATE CONSTRAINT TRIGGER trg_exit_handoff_card_shape" in guards
    assert "CREATE CONSTRAINT TRIGGER trg_exit_handoff_card_rung_shape" in guards
    assert guards.count("DEFERRABLE INITIALLY DEFERRED") == 2
    # An untagged proposal never runs the parent trigger's function.
    assert "FOR EACH ROW WHEN (NEW.exit_handoff_id IS NOT NULL)" in guards


@pytest.mark.parametrize(
    "model",
    [
        ExecutionAccountControl,
        ExecutionMandate,
        ExecutionMandateRevocation,
        ExecutionOperatorExit,
        ExecutionExitHandoff,
        ExecutionChildOrderEvidence,
    ],
)
def test_new_table_ddl_matches_the_model(model: type) -> None:
    table: sa.Table = model.__table__  # type: ignore[attr-defined]
    block = next(
        statement
        for statement in _migration().TABLE_DDL
        if f"CREATE TABLE IF NOT EXISTS review.{table.name} (" in statement
    )
    for column in table.columns:
        assert f" {column.name} " in block, column.name
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert checks
    for check in checks:
        assert f"CONSTRAINT {check.name}" in block, check.name
        assert f"CHECK ({check.sqltext})" in block, str(check.sqltext)
    foreign_keys = [
        c for c in table.constraints if isinstance(c, sa.ForeignKeyConstraint)
    ]
    for foreign_key in foreign_keys:
        assert f"CONSTRAINT {foreign_key.name}" in block, foreign_key.name


def test_altered_tables_match_their_models() -> None:
    from app.models.order_proposals import OrderProposal
    from app.models.strategy_sleeves import StrategySleeve

    alters = "\n".join(_migration().ALTER_DDL)
    assert "review.strategy_sleeves ADD COLUMN IF NOT EXISTS execution_path" in alters
    assert "review.order_proposals ADD COLUMN IF NOT EXISTS exit_handoff_id" in alters
    path = StrategySleeve.__table__.c.execution_path  # type: ignore[attr-defined]
    assert path.nullable is False and path.server_default.arg == "card"
    handoff = OrderProposal.__table__.c.exit_handoff_id  # type: ignore[attr-defined]
    assert handoff.nullable is True
    [foreign_key] = handoff.foreign_keys
    assert foreign_key.target_fullname == "review.execution_exit_handoffs.id"
    assert foreign_key.constraint.name in alters


# ------------------------------------------------------------ schema v2 (§3.2, §3.3)


def _v2(**overrides: Any) -> dict[str, Any]:
    mandate_id = uuid4()
    values: dict[str, Any] = {
        "schema_version": 2,
        "intent_id": str(uuid4()),
        "strategy": "upbit-trend-long-v1",
        "account": "upbit_live",
        "venue": "upbit",
        "execution_mode": "shadow",
        "mandate_id": str(mandate_id),
        "sleeve_id": 7,
        "symbol": "KRW-BTC",
        "side": "buy",
        "max_quantity_decimal": "0.00320000",
        "max_notional_krw": 480_000,
        "strategy_code_sha": "a" * 40,
        "limit_price_krw": 150_000_000,
        "max_slippage_bps": 30,
        "expires_at": (NOW + timedelta(hours=6)).isoformat(),
        "inventory": {
            "position_at_authorization": "0",
            "min_position_after": "0",
            "max_position_after": "0.0032",
        },
        "policy_version": "upbit-trend-long-v1",
        "authorization_ref": f"mandate:{mandate_id}",
        "decision_ref": "upbit-trend:KRW-BTC:2026-11-20",
    }
    return {**values, **overrides}


def test_a_v2_intent_validates_and_has_a_stable_digest() -> None:
    payload = _v2()
    first = ExecutionIntentV2.model_validate(payload)
    assert contract_digest(first) == contract_digest(
        ExecutionIntentV2.model_validate(payload)
    )
    assert first.origin == "strategy" and first.operator_exit_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution_mode": "paper"},
        {"account": "kis_mock"},
        {"venue": "krx"},
        {"symbol": "BTC"},
        {"symbol": "USDT-BTC"},
        {"max_quantity_decimal": "0.123456789"},
        {"max_quantity_decimal": "0"},
        {"max_quantity_decimal": 1},
        {"max_notional_krw": 4_999},
        {"strategy_code_sha": "A" * 40},
        {"limit_price_krw": 150_000_500},
        {"limit_price_krw": 50},
        {"max_slippage_bps": 501},
        {"authorization_ref": "mandate:someone-else"},
        {"max_quantity": 3},
        {"decision_ref": ""},
        {"origin": "operator_exit"},
    ],
)
def test_a_v2_intent_outside_the_contract_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ExecutionIntentV2.model_validate(_v2(**overrides))


def test_a_v2_intent_has_no_default_execution_mode() -> None:
    payload = _v2()
    payload.pop("execution_mode")
    with pytest.raises(ValidationError):
        ExecutionIntentV2.model_validate(payload)


def test_an_operator_exit_intent_is_a_sell_that_names_its_exit_row() -> None:
    exit_id = uuid4()
    payload = _v2(
        side="sell",
        origin="operator_exit",
        operator_exit_id=str(exit_id),
        authorization_ref=f"operator-exit:{exit_id}",
    )
    assert ExecutionIntentV2.model_validate(payload).origin == "operator_exit"
    with pytest.raises(ValidationError):
        ExecutionIntentV2.model_validate({**payload, "side": "buy"})


def test_v1_does_not_accept_the_v2_account_or_fields() -> None:
    v1 = {
        "schema_version": 1,
        "intent_id": str(uuid4()),
        "strategy": "s3-test",
        "account": "kis_mock",
        "venue": "krx",
        "symbol": "005930",
        "side": "buy",
        "max_quantity": 10,
        "max_notional_krw": 1_000_000,
        "limit_price_krw": 70_000,
        "max_slippage_bps": 100,
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "inventory": {
            "position_at_authorization": 0,
            "min_position_after": 0,
            "max_position_after": 10,
        },
        "policy_version": "p1",
        "authorization_ref": "auth-1",
    }
    ExecutionIntentV1.model_validate(v1)
    for bad in ({"account": "upbit_live"}, {"max_quantity_decimal": "1"}):
        with pytest.raises(ValidationError):
            ExecutionIntentV1.model_validate({**v1, **bad})


def test_the_child_shape_is_chosen_by_quantity_decimal_only() -> None:
    base = {
        "action": "submit",
        "intent_id": str(uuid4()),
        "idempotency_key": "child-0001",
    }
    kis = parse_child_request({**base, "quantity": 3, "limit_price_krw": 70_100})
    upbit = parse_child_request(
        {**base, "quantity_decimal": "0.5", "limit_price_krw": 3_000}
    )
    assert type(kis) is SubmitChild and type(upbit) is UpbitSubmitChild
    for mixed in (
        {**base, "quantity": 3, "quantity_decimal": "3", "limit_price_krw": 3_000},
        {**base, "quantity_decimal": 0.5, "limit_price_krw": 3_000},
        {**base, "quantity_decimal": "0.5", "limit_price_krw": 3_000.0},
        {**base, "quantity_decimal": "1e2", "limit_price_krw": 3_000},
    ):
        with pytest.raises(ValidationError):
            parse_child_request(mixed)


# ------------------------------------------------------------ S3, §8.2-5


def test_s3_a_mandate_has_no_calendar_day_cap() -> None:
    columns = {column.name for column in ExecutionMandate.__table__.columns}  # type: ignore[attr-defined]
    assert not {name for name in columns if "daily" in name or "per_day" in name}
    assert "per_intent_max_notional_krw" in columns and "max_open_intents" in columns


def test_the_loss_sell_guard_cooldown_and_loss_cut_are_not_bypassed_from_here() -> None:
    """§8.2-5: this path reads the guards' inputs and never passes a bypass.

    The forwarder calls the order function with none of the arguments that
    switch a guard off, and no module of the package names one.
    """
    bypasses = (
        "defensive_trim",
        "scalping_exit",
        "cash_funding_target",
        "parking_sell_ctx",
        "exit_intent",
        "allow_loss_sell",
        "approval_issue_id",
        "retrospective_id",
    )
    for path in sorted(PACKAGE.glob("upbit_*.py")):
        source = path.read_text("utf-8")
        for name in bypasses:
            assert name not in source, (path.name, name)


def _method(path: Path, class_name: str, method: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == method:
                    return item
    raise AssertionError(f"{class_name}.{method} not found in {path.name}")


def _awaited_calls(function: ast.AsyncFunctionDef) -> list[str]:
    """Every awaited call of ``function`` in source order, as its dotted name."""
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    ]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    return [ast.unparse(node.value.func) for node in calls]


@pytest.mark.parametrize(
    ("module", "class_name", "method"),
    [
        ("upbit_store.py", "UpbitGatewayStore", "reserve"),
        ("store.py", "SqlAlchemyGatewayStore", "reserve"),
    ],
)
def test_every_reservation_takes_the_journal_key_lock_first(
    module: str, class_name: str, method: str
) -> None:
    """PR 2486 review (65wMjP): the shape refusal decides on a key under its
    lock. That only means something if every writer of the journal -- both
    accounts share it -- takes the same lock before it reads or locks anything
    else. The waiting itself is exercised against PostgreSQL in CI
    (``test_upbit_db.py``, ``test_store_db.py``)."""
    awaited = _awaited_calls(_method(PACKAGE / module, class_name, method))
    assert awaited[0] == "lock_child_key", awaited[:3]


def test_the_shape_refusal_reads_the_key_only_under_its_lock() -> None:
    lookup = _method(
        PACKAGE / "upbit_store.py", "UpbitGatewayStore", "key_is_journaled"
    )
    awaited = _awaited_calls(lookup)
    assert awaited.index("lock_child_key") < awaited.index("session.scalar")
    source = ast.unparse(lookup)
    # A new statement after the wait must see what the waited-for reservation
    # committed, and a wait that cannot end must not turn into a refusal.
    assert "ISOLATION LEVEL READ COMMITTED" in source
    assert "lock_timeout" in source
    assert "except" not in source
