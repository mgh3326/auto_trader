"""#728 read-only protected_positions CLI against the pytest-owned database."""

from __future__ import annotations

import ast
from argparse import Namespace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.db import engine
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    ProtectedQuantityService,
)
from scripts import protected_positions
from tests._run_owned_database import validate_run_owned_database_url


def _provider():
    async def observe() -> BrokerPositionObservation:
        return BrokerPositionObservation(
            held=Decimal("10"),
            sellable=Decimal("8"),
            observed_at=datetime.now(UTC),
        )

    return observe


@pytest.mark.unit
def test_cli_is_explicit_database_read_only_and_has_no_write_command() -> None:
    parser = protected_positions._parser()
    command_action = next(
        action for action in parser._actions if action.dest == "command"
    )
    choices = set(command_action.choices)
    source = Path(protected_positions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert parser.parse_args(
        ["--database-url", "postgresql://u:p@localhost/db", "list"]
    ).database_url
    assert choices == {"list", "show", "history"}
    assert "save" not in choices and "write" not in choices
    assert not any("brokers" in module for module in imports)
    assert ".save(" not in source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_list_show_history_and_not_found_use_only_explicit_test_database(
    db_session,
) -> None:
    validate_run_owned_database_url(engine.url)
    symbol = f"TCLI{uuid4().hex[:8].upper()}"
    await ProtectedQuantityService(db_session).save(
        account_scope="kis_live",
        market="kr",
        symbol=symbol,
        protected_quantity="6.25",
        expected_revision=None,
        reason="cli visibility test",
        idempotency_key=f"cli-{uuid4()}",
        actor_user_id=728101,
        origin="invest_ui",
        observation_provider=_provider(),
        confirm_protection_change=True,
    )
    database_url = engine.url.render_as_string(hide_password=False)
    list_code, listed = await protected_positions.read_command(
        Namespace(command="list", account_scope="kis_live", database_url=database_url)
    )
    show_code, shown = await protected_positions.read_command(
        Namespace(
            command="show",
            account_scope="kis_live",
            market="kr",
            symbol=symbol,
            database_url=database_url,
        )
    )
    history_code, history = await protected_positions.read_command(
        Namespace(
            command="history",
            account_scope="kis_live",
            market="kr",
            symbol=symbol,
            database_url=database_url,
        )
    )
    missing_code, missing = await protected_positions.read_command(
        Namespace(
            command="show",
            account_scope="kis_live",
            market="kr",
            symbol="TNONE728",
            database_url=database_url,
        )
    )
    assert list_code == show_code == history_code == 0
    assert any(item["symbol"] == symbol for item in listed["positions"])
    assert shown["position"]["protected_quantity"] == "6.25000000"
    assert history["history"][0]["reason"] == "cli visibility test"
    assert missing_code == 2
    assert missing["error"] == "not_found"


@pytest.mark.unit
def test_cli_text_rendering_is_human_readable_and_json_order_is_stable() -> None:
    assert (
        protected_positions._render_text({"positions": []}) == "no protected positions"
    )
    rendered = protected_positions._render_text(
        {
            "positions": [
                {
                    "account_scope": "kis_live",
                    "market": "kr",
                    "symbol": "005930",
                    "protected_quantity": "2",
                    "revision": 1,
                }
            ]
        }
    )
    assert rendered == "kis_live kr 005930 P=2 revision=1"
