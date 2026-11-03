"""The manual alert check is SELECT-only and fails visibly."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import diagnose_krx_after_market_eligibility as cli


@pytest.mark.parametrize("status", ["fresh", "stale", "missing"])
@pytest.mark.asyncio
async def test_check_exit_and_read_only_transaction(monkeypatch, capsys, status):
    conn = MagicMock()
    conn.execution_options = AsyncMock(return_value=conn)
    conn.execute = AsyncMock()
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = conn
    engine.dispose = AsyncMock()
    monkeypatch.setattr(cli, "engine", engine)
    monkeypatch.setattr(
        cli,
        "load_krx_after_market_health",
        AsyncMock(
            return_value={"status": status, "requires_attention": status != "fresh"}
        ),
    )
    assert await cli.run() == (0 if status == "fresh" else 1)
    conn.execution_options.assert_awaited_once_with(postgresql_readonly=True)
    assert str(conn.execute.call_args.args[0]).startswith("SELECT set_config(")
    assert status in capsys.readouterr().out
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_failure_is_nonzero_and_sanitized(monkeypatch, capsys):
    engine = MagicMock()
    engine.connect.return_value.__aenter__.side_effect = RuntimeError("private DSN")
    engine.dispose = AsyncMock()
    monkeypatch.setattr(cli, "engine", engine)
    assert await cli.run() == 1
    output = capsys.readouterr().out
    assert '"status": "unavailable"' in output
    assert '"error_class": "RuntimeError"' in output
    assert "private DSN" not in output
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_failure_is_nonzero_and_sanitized(monkeypatch, capsys):
    engine = MagicMock()
    engine.connect.return_value.__aenter__.side_effect = RuntimeError("private DSN")
    engine.dispose = AsyncMock(side_effect=RuntimeError("private cleanup detail"))
    monkeypatch.setattr(cli, "engine", engine)
    assert await cli.run() == 1
    output = capsys.readouterr().out
    assert '"status": "unavailable"' in output
    assert "private DSN" not in output
    assert "private cleanup detail" not in output
