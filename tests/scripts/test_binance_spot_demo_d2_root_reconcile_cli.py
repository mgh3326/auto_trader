"""#1268 — CLI argument, database-name and client handling (no DB, no network)."""

from __future__ import annotations

import json

import pytest

from scripts import binance_spot_demo_d2_root_reconcile as cli
from tests.services.brokers.binance.spot_demo._d2_root_fixtures import (
    FakeSpotDemoReader,
)

pytestmark = pytest.mark.unit

BASE = ["--ids", "442,443,444", "--reason", "hk 1268", "--actor", "desk"]


def _main(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


@pytest.mark.parametrize(
    "argv",
    [
        ["--database-url-env", "X", "--reason", "r", "--actor", "a"],
        ["--database-url-env", "X", "--ids", "442", "--actor", "a"],
        ["--database-url-env", "X", "--ids", "442", "--reason", "r"],
        ["--ids", "442", "--reason", "r", "--actor", "a"],
        ["--database-url", "u", "--database-url-env", "X", *BASE],
    ],
)
def test_required_arguments(argv) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli._parser().parse_args(argv)
    assert excinfo.value.code == 2


@pytest.mark.parametrize("ids", ["442-444", "442,442", "442,443,444,445", "0", "+1"])
def test_bad_ids_exit_1_before_any_client(ids, capsys, monkeypatch) -> None:
    def boom():
        raise AssertionError("client must not be built")

    monkeypatch.setattr(cli, "_default_client_factory", boom)
    code, out, err = _main(
        ["--database-url-env", "X", "--ids", ids, "--reason", "r", "--actor", "a"],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert "--ids" in json.loads(err)["error"]


def test_blank_reason_exit_1(capsys) -> None:
    code, _, err = _main(
        ["--database-url-env", "X", "--ids", "442", "--reason", " ", "--actor", "a"],
        capsys,
    )
    assert code == 1
    assert "reason" in json.loads(err)["error"]


def test_missing_database_env_names_the_variable_only(capsys, monkeypatch) -> None:
    monkeypatch.delenv("D2_RECONCILE_TEST_DB_URL", raising=False)
    code, _, err = _main(
        ["--database-url-env", "D2_RECONCILE_TEST_DB_URL", *BASE], capsys
    )
    assert code == 1
    assert "D2_RECONCILE_TEST_DB_URL" in json.loads(err)["error"]


def test_bad_database_url_value_is_never_echoed(capsys, monkeypatch) -> None:
    monkeypatch.setenv("D2_RECONCILE_TEST_DB_URL", "mysql://user:hunter2@h/db")
    code, out, err = _main(
        ["--database-url-env", "D2_RECONCILE_TEST_DB_URL", *BASE], capsys
    )
    assert code == 1
    assert "hunter2" not in out + err


def test_client_construction_failure_reports_class_only(capsys, monkeypatch) -> None:
    monkeypatch.setenv(
        "D2_RECONCILE_TEST_DB_URL", "postgresql+asyncpg://u:hunter2@127.0.0.1:1/db"
    )

    class SpotDemoDisabled(RuntimeError):
        pass

    def factory():
        raise SpotDemoDisabled("secret-ish detail hunter2")

    monkeypatch.setattr(cli, "_default_client_factory", factory)
    code, out, err = _main(
        ["--database-url-env", "D2_RECONCILE_TEST_DB_URL", *BASE], capsys
    )
    assert code == 1
    assert json.loads(err) == {"error": "error:SpotDemoDisabled"}
    assert "hunter2" not in out + err


@pytest.mark.parametrize(
    "client",
    [
        FakeSpotDemoReader({}, base_url="https://api.binance.com"),
        FakeSpotDemoReader({}, base_url="https://demo-fapi.binance.com"),
        FakeSpotDemoReader({}, credential_fingerprint="sha256:" + "2" * 64),
    ],
)
@pytest.mark.asyncio
async def test_wrong_host_or_account_refuses_before_reads(client) -> None:
    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    args = cli._parser().parse_args(["--database-url-env", "X", *BASE])
    with pytest.raises(Exception) as excinfo:
        await cli.run(args, session_factory=_Session, client_factory=lambda: client)
    assert type(excinfo.value).__name__ == "D2RootReconcileInputError"
    assert client.calls == []
    assert client.closed


def test_default_client_is_the_spot_demo_execution_client() -> None:
    import inspect

    source = inspect.getsource(cli._default_client_factory)
    assert "BinanceSpotDemoExecutionClient.from_env()" in source
