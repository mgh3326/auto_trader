"""#728 read-only protected_positions CLI against the pytest-owned database."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
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
def test_cli_read_path_is_explicit_database_only_and_writes_are_reviewed() -> None:
    parser = protected_positions._parser()
    command_action = next(
        action for action in parser._actions if action.dest == "command"
    )
    choices = set(command_action.choices)
    source = Path(protected_positions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Module-level imports only: the read commands must stay broker- and
    # settings-free; the #943 write commands import those lazily.
    module_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    module_imports.update(
        node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)
    )
    assert parser.parse_args(
        ["--database-url", "postgresql://u:p@localhost/db", "list"]
    ).database_url
    assert choices == {
        "list",
        "show",
        "history",
        "declare",
        "increase",
        "decrease",
        "release",
        "auto-reconcile",
    }
    assert (
        parser.parse_args(
            ["--database-url", "postgresql://u:p@localhost/db", "auto-reconcile"]
        ).commit
        is False
    )
    assert not any("brokers" in module for module in module_imports)
    assert not any("config" in module for module in module_imports)
    assert not any("protected_position_settings" in m for m in module_imports)
    assert source.count(".save(") == 1
    assert 'origin="operator_cli"' in source
    for command in ("declare", "increase", "decrease", "release"):
        base = ["--database-url", "postgresql://u:p@localhost/db", command]
        base += ["kis_live", "kr", "005930", "--reason", "r"]
        if command != "release":
            base += ["--quantity", "1"]
        if command != "declare":
            base += ["--expected-revision", "1"]
        with pytest.raises(SystemExit):
            parser.parse_args(base)  # --confirm-symbol is mandatory
        args = parser.parse_args([*base, "--confirm-symbol", "005930"])
        assert args.commit is False  # dry-run unless --commit


@pytest.mark.integration
def test_cli_subprocess_reads_explicit_database_without_broker_secrets(
    db_session,
) -> None:
    validate_run_owned_database_url(engine.url)
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "protected_positions.py"),
            "--database-url",
            engine.url.render_as_string(hide_password=False),
            "show",
            "kis_live",
            "kr",
            f"TNONE{uuid4().hex[:8].upper()}",
        ],
        cwd=project_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "ENV_FILE": "/dev/null",
            "DEV_ENV_FILE": "/dev/null",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["error"] == "not_found"


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


class _Broker:
    """Fake fresh-observation provider; counts reads, never reaches a broker."""

    def __init__(self, held: str = "10") -> None:
        self.held = Decimal(held)
        self.reads = 0

    def factory(self, key):
        async def observe() -> BrokerPositionObservation:
            self.reads += 1
            return BrokerPositionObservation(
                held=self.held, sellable=self.held, observed_at=datetime.now(UTC)
            )

        return observe


def _write_args(command: str, symbol: str, **overrides) -> Namespace:
    values = {
        "command": command,
        "database_url": engine.url.render_as_string(hide_password=False),
        "account_scope": "kis_live",
        "market": "kr",
        "symbol": symbol,
        "quantity": "3",
        "expected_revision": None,
        "reason": "desk declaration",
        "confirm_symbol": symbol,
        "idempotency_key": None,
        "commit": False,
    }
    values.update(overrides)
    return Namespace(**values)


async def _head(symbol: str):
    from app.core.db import AsyncSessionLocal
    from app.services.protected_quantity_service import normalize_protection_key

    key = normalize_protection_key(account_scope="kis_live", market="kr", symbol=symbol)
    async with AsyncSessionLocal() as db:
        service = ProtectedQuantityService(db)
        return await service.get(key=key), await service.list_revisions(key=key)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_write_is_dry_run_by_default_and_writes_nothing(db_session) -> None:
    validate_run_owned_database_url(engine.url)
    symbol = f"TW{uuid4().hex[:8].upper()}"
    broker = _Broker("5")

    code, payload = await protected_positions.write_command(
        _write_args("declare", symbol), provider_factory=broker.factory
    )

    head, _ = await _head(symbol)
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["preview"]["after_protected_quantity"] == "3"
    assert payload["exceeds_broker_held"] is False
    assert head is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_commit_writes_through_service_as_operator_cli_owner(
    db_session,
) -> None:
    from app.services.protected_position_auto_follow import protection_owner_user_id

    validate_run_owned_database_url(engine.url)
    symbol = f"TW{uuid4().hex[:8].upper()}"
    broker = _Broker("5")

    declared = await protected_positions.write_command(
        _write_args("declare", symbol, commit=True), provider_factory=broker.factory
    )
    increased = await protected_positions.write_command(
        _write_args("increase", symbol, quantity="5", expected_revision=1, commit=True),
        provider_factory=broker.factory,
    )
    decreased = await protected_positions.write_command(
        _write_args("decrease", symbol, quantity="2", expected_revision=2, commit=True),
        provider_factory=broker.factory,
    )
    released = await protected_positions.write_command(
        _write_args("release", symbol, expected_revision=3, commit=True),
        provider_factory=broker.factory,
    )

    head, revisions = await _head(symbol)
    assert [code for code, _ in (declared, increased, decreased, released)] == [0] * 4
    assert [row.action for row in revisions] == [
        "declare",
        "increase",
        "decrease",
        "release",
    ]
    assert {row.origin for row in revisions} == {"operator_cli"}
    assert {row.actor_user_id for row in revisions} == {protection_owner_user_id()}
    assert head is not None and head.protected_quantity == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_write_refusals_happen_before_any_revision(db_session) -> None:
    validate_run_owned_database_url(engine.url)
    symbol = f"TW{uuid4().hex[:8].upper()}"
    broker = _Broker("5")
    await protected_positions.write_command(
        _write_args("declare", symbol, commit=True), provider_factory=broker.factory
    )
    reads_after_declare = broker.reads

    mismatch = await protected_positions.write_command(
        _write_args(
            "increase",
            symbol,
            quantity="4",
            expected_revision=1,
            confirm_symbol="005930",
            commit=True,
        ),
        provider_factory=broker.factory,
    )
    assert mismatch[0] == 2 and mismatch[1]["error"] == "symbol_confirmation_mismatch"
    stale = await protected_positions.write_command(
        _write_args("increase", symbol, quantity="4", expected_revision=9, commit=True),
        provider_factory=broker.factory,
    )
    assert stale[0] == 3 and stale[1]["error"] == "stale_form"
    wrong_word = await protected_positions.write_command(
        _write_args("decrease", symbol, quantity="4", expected_revision=1, commit=True),
        provider_factory=broker.factory,
    )
    assert wrong_word[0] == 2 and wrong_word[1]["error"] == "command_mismatch"
    redeclare = await protected_positions.write_command(
        _write_args("declare", symbol, commit=True), provider_factory=broker.factory
    )
    assert redeclare[0] == 2 and redeclare[1]["error"] == "command_mismatch"
    with pytest.raises(protected_positions.ProtectedQuantityValidationError):
        await protected_positions.write_command(
            _write_args(
                "increase", symbol, quantity="6", expected_revision=1, commit=True
            ),
            provider_factory=broker.factory,
        )

    head, revisions = await _head(symbol)
    # Only the above-held increase reached the in-lock broker read.
    assert broker.reads == reads_after_declare + 1
    assert head is not None and head.protected_quantity == Decimal("3")
    assert len(revisions) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_lever_previews_by_default_and_commits_only_with_flag(
    db_session,
) -> None:
    from types import SimpleNamespace

    validate_run_owned_database_url(engine.url)
    symbol = f"TL{uuid4().hex[:8].upper()}"
    await protected_positions.write_command(
        _write_args("declare", symbol, quantity="4", commit=True),
        provider_factory=_Broker("4").factory,
    )
    messages: list[str] = []

    async def notify(message: str) -> None:
        messages.append(message)

    class AppSale(_Broker):
        def factory(self, key):
            self.held = Decimal("1") if key.symbol == symbol else Decimal("100000")
            return super().factory(key)

    def lever(**overrides):
        values = {
            "command": "auto-reconcile",
            "database_url": engine.url.render_as_string(hide_password=False),
            "account_scope": "kis_live",
            "commit": False,
        }
        values.update(overrides)
        return Namespace(**values)

    on = SimpleNamespace(protected_position_auto_follow_enabled=True)
    off = SimpleNamespace(protected_position_auto_follow_enabled=False)
    disabled = await protected_positions.lever_command(
        lever(commit=True),
        provider_factory=AppSale().factory,
        notify=notify,
        settings_obj=off,
    )
    preview = await protected_positions.lever_command(
        lever(), provider_factory=AppSale().factory, notify=notify, settings_obj=on
    )
    after_preview, _ = await _head(symbol)
    committed = await protected_positions.lever_command(
        lever(commit=True),
        provider_factory=AppSale().factory,
        notify=notify,
        settings_obj=on,
    )
    head, revisions = await _head(symbol)

    assert disabled[0] == 2 and disabled[1]["error"] == "auto_follow_disabled"
    [mine] = [o for o in preview[1]["outcomes"] if o["symbol"] == symbol]
    assert preview[0] == 0 and mine["status"] == "would_lower"
    assert after_preview is not None and after_preview.protected_quantity == 4
    [mine] = [o for o in committed[1]["outcomes"] if o["symbol"] == symbol]
    assert committed[0] == 0 and mine["status"] == "lowered"
    assert head is not None and head.protected_quantity == 1
    assert revisions[-1].reason == "auto:reconcile"
    assert sum(symbol in message for message in messages) == 1


# --- #943 round 3: --database-url-env (timer form keeps the URL out of argv) ---

_SECRET = "Sup3rSecretT943"


def _url() -> str:
    return engine.url.render_as_string(hide_password=False)


def _assert_no_leak(text: str) -> None:
    assert _url() not in text
    assert _SECRET not in text
    password = engine.url.password
    if password:
        assert str(password) not in text


@pytest.mark.unit
def test_cli_database_selection_is_exactly_one_of_url_or_env_name() -> None:
    parser = protected_positions._parser()
    env_args = parser.parse_args(["--database-url-env", "PP_DB", "list"])
    assert (env_args.database_url_env, env_args.database_url) == ("PP_DB", None)
    with pytest.raises(SystemExit):
        parser.parse_args(["list"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--database-url",
                "postgresql://u:p@h/db",
                "--database-url-env",
                "X",
                "list",
            ]
        )


@pytest.mark.integration
def test_cli_env_form_reads_the_named_variable_and_never_prints_it(
    db_session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    validate_run_owned_database_url(engine.url)
    monkeypatch.setenv("T943_PP_DB_URL", _url())

    listed = protected_positions.main(
        ["--database-url-env", "T943_PP_DB_URL", "list", "--scope", "kis_live"]
    )
    list_out = capsys.readouterr()
    missing = protected_positions.main(
        ["--database-url-env", "T943_PP_DB_URL", "show", "kis_live", "kr", "TNONE943"]
    )
    show_out = capsys.readouterr()
    # The lever resolves the database the same way; the kill switch is off in
    # tests, so it reports disabled without any broker read.
    lever = protected_positions.main(
        ["--database-url-env", "T943_PP_DB_URL", "auto-reconcile", "--commit"]
    )
    lever_out = capsys.readouterr()

    assert listed == 0 and "positions" in json.loads(list_out.out)
    assert missing == 2 and json.loads(show_out.out)["error"] == "not_found"
    assert lever == 2 and json.loads(lever_out.out)["error"] == "auto_follow_disabled"
    for captured in (list_out, show_out, lever_out):
        _assert_no_leak(captured.out + captured.err)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "environment variable T943_PP_DB_URL is not set or empty"),
        ("   ", "environment variable T943_PP_DB_URL is not set or empty"),
        (
            f"postgresql+asyncpg://user:{_SECRET}@localhost/",
            "environment variable T943_PP_DB_URL does not hold a complete "
            "PostgreSQL URL",
        ),
        (
            f"::: not a url {_SECRET} :::",
            "environment variable T943_PP_DB_URL does not hold a complete "
            "PostgreSQL URL",
        ),
        (
            f"mysql://user:{_SECRET}@localhost/db",
            "environment variable T943_PP_DB_URL does not hold a complete "
            "PostgreSQL URL",
        ),
    ],
)
def test_cli_env_form_errors_name_only_the_variable(
    value: str | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if value is None:
        monkeypatch.delenv("T943_PP_DB_URL", raising=False)
    else:
        monkeypatch.setenv("T943_PP_DB_URL", value)

    for argv in (
        ["list"],
        ["auto-reconcile", "--commit"],
        [
            "declare",
            "kis_live",
            "kr",
            "005930",
            "--quantity",
            "1",
            "--reason",
            "r",
            "--confirm-symbol",
            "005930",
            "--commit",
        ],
    ):
        code = protected_positions.main(["--database-url-env", "T943_PP_DB_URL", *argv])
        captured = capsys.readouterr()
        assert code == 2
        assert json.loads(captured.out) == {
            "error": "invalid_request",
            "message": message,
        }
        _assert_no_leak(captured.out + captured.err)


@pytest.mark.unit
def test_cli_env_form_rejects_a_malformed_variable_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = protected_positions.main(["--database-url-env", "BAD-NAME;x", "list"])
    captured = capsys.readouterr()
    assert code == 2
    assert "environment variable name" in json.loads(captured.out)["message"]


@pytest.mark.integration
def test_cli_subprocess_env_form_keeps_url_out_of_argv_and_output(db_session) -> None:
    validate_run_owned_database_url(engine.url)
    project_root = Path(__file__).resolve().parents[2]
    argv = [
        sys.executable,
        str(project_root / "scripts" / "protected_positions.py"),
        "--database-url-env",
        "T943_PP_DB_URL",
        "show",
        "kis_live",
        "kr",
        f"TNONE{uuid4().hex[:8].upper()}",
    ]
    result = subprocess.run(
        argv,
        cwd=project_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "ENV_FILE": "/dev/null",
            "DEV_ENV_FILE": "/dev/null",
            "T943_PP_DB_URL": _url(),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["error"] == "not_found"
    assert not any(_url() in part for part in argv)
    _assert_no_leak(result.stdout + result.stderr)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_lever_exits_partial_failure_when_a_key_is_unobserved(
    db_session,
) -> None:
    """#1061: the good key is judged, the unobserved key keeps P, exit is 1."""

    from types import SimpleNamespace

    from app.services.protected_position_settings import (
        PositionObservationUnavailable,
    )

    validate_run_owned_database_url(engine.url)
    good = f"TG{uuid4().hex[:8].upper()}"
    unread = f"TU{uuid4().hex[:8].upper()}"
    for symbol in (good, unread):
        await protected_positions.write_command(
            _write_args(
                "declare", symbol, quantity="4", commit=True, account_scope="toss_live"
            ),
            provider_factory=_Broker("4").factory,
        )

    class Mixed(_Broker):
        def factory(self, key):
            if key.symbol == unread:

                async def unobserved() -> BrokerPositionObservation:
                    raise PositionObservationUnavailable(
                        key=key, field="sellable_quantity"
                    )

                return unobserved
            self.held = Decimal("1") if key.symbol == good else Decimal("100000")
            return super().factory(key)

    messages: list[str] = []

    async def notify(message: str) -> None:
        messages.append(message)

    code, payload = await protected_positions.lever_command(
        Namespace(
            command="auto-reconcile",
            database_url=engine.url.render_as_string(hide_password=False),
            account_scope="toss_live",
            commit=True,
        ),
        provider_factory=Mixed().factory,
        notify=notify,
        settings_obj=SimpleNamespace(protected_position_auto_follow_enabled=True),
    )

    [good_item] = [o for o in payload["outcomes"] if o["symbol"] == good]
    [unread_item] = [o for o in payload["outcomes"] if o["symbol"] == unread]
    assert code == 1
    assert good_item["status"] == "lowered"
    assert unread_item["status"] == "unobserved"
    assert unread_item["reason"] == f"sellable_quantity_unavailable:{unread}"
    assert not any(unread in message for message in messages)
    from app.core.db import AsyncSessionLocal
    from app.services.protected_quantity_service import ProtectionKey

    async with AsyncSessionLocal() as db:
        service = ProtectedQuantityService(db)
        unread_head = await service.get(key=ProtectionKey("toss_live", "kr", unread))
        good_head = await service.get(key=ProtectionKey("toss_live", "kr", good))
    assert unread_head is not None and unread_head.protected_quantity == 4
    assert unread_head.revision == 1
    assert good_head is not None and good_head.protected_quantity == 1
