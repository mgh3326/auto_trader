"""Unit tests for ROB-94 MCP wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling.weekend_crypto_paper_cycle import (
    WEEKEND_CRYPTO_PAPER_CYCLE_TOOL_NAMES,
    register_weekend_crypto_paper_cycle_tools,
    weekend_crypto_paper_cycle_run,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_tool_is_dry_run():
    mock_report = MagicMock()
    mock_report.to_dict.return_value = {
        "status": "dry_run_ok",
        "dry_run": True,
        "confirm": False,
        "candidates_seen": 0,
        "candidates_selected": 0,
        "candidates_completed": 0,
        "candidates_blocked": 0,
        "traces": [],
        "cycle_anomalies": [],
        "checked_at": "2026-05-04T10:00:00+00:00",
    }
    mock_runner = MagicMock()
    mock_runner.run_cycle = AsyncMock(return_value=mock_report)

    with patch(
        "app.mcp_server.tooling.weekend_crypto_paper_cycle.WeekendCryptoPaperCycleRunner",
        return_value=mock_runner,
    ):
        result = await weekend_crypto_paper_cycle_run()

    assert result["dry_run"] is True
    mock_runner.run_cycle.assert_awaited_once_with(
        dry_run=True,
        confirm=False,
        max_candidates=3,
        symbols=None,
        approval_tokens=None,
        operator_token=None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_execute_without_operator_token_returns_gate_refused():
    result = await weekend_crypto_paper_cycle_run(dry_run=False, confirm=True)
    assert result["status"] == "gate_refused"
    assert result["dry_run"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_execute_without_confirm_returns_gate_refused():
    result = await weekend_crypto_paper_cycle_run(
        dry_run=False,
        confirm=False,
        operator_token="operator",
        approval_tokens={},
    )
    assert result["status"] == "gate_refused"
    assert result["confirm"] is False


@pytest.mark.unit
def test_tool_name_constant_contains_registered_tool():
    assert WEEKEND_CRYPTO_PAPER_CYCLE_TOOL_NAMES == {"weekend_crypto_paper_cycle_run"}


@pytest.mark.unit
def test_register_weekend_crypto_paper_cycle_tool():
    registered = {}

    class FakeMcp:
        def tool(self, *, name, description):
            def _decorator(fn):
                registered[name] = {"description": description, "fn": fn}
                return fn

            return _decorator

    register_weekend_crypto_paper_cycle_tools(FakeMcp())
    assert (
        registered["weekend_crypto_paper_cycle_run"]["fn"]
        is weekend_crypto_paper_cycle_run
    )
    assert "dry-run" in registered["weekend_crypto_paper_cycle_run"]["description"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_handles_unexpected_runner_error_gracefully():
    mock_runner = MagicMock()
    mock_runner.run_cycle = AsyncMock(side_effect=RuntimeError("unexpected boom"))

    with patch(
        "app.mcp_server.tooling.weekend_crypto_paper_cycle.WeekendCryptoPaperCycleRunner",
        return_value=mock_runner,
    ):
        result = await weekend_crypto_paper_cycle_run()

    assert result == {
        "status": "error",
        "error": "RuntimeError",
        "detail": "unexpected boom",
        "dry_run": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_passes_max_candidates_to_runner():
    mock_report = MagicMock()
    mock_report.to_dict.return_value = {"status": "dry_run_ok", "dry_run": True}
    mock_runner = MagicMock()
    mock_runner.run_cycle = AsyncMock(return_value=mock_report)

    with patch(
        "app.mcp_server.tooling.weekend_crypto_paper_cycle.WeekendCryptoPaperCycleRunner",
        return_value=mock_runner,
    ):
        await weekend_crypto_paper_cycle_run(max_candidates=2)

    assert mock_runner.run_cycle.await_args.kwargs["max_candidates"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_passes_symbols_filter_to_runner():
    mock_report = MagicMock()
    mock_report.to_dict.return_value = {"status": "dry_run_ok", "dry_run": True}
    mock_runner = MagicMock()
    mock_runner.run_cycle = AsyncMock(return_value=mock_report)

    with patch(
        "app.mcp_server.tooling.weekend_crypto_paper_cycle.WeekendCryptoPaperCycleRunner",
        return_value=mock_runner,
    ):
        await weekend_crypto_paper_cycle_run(symbols=["BTC/USD"])

    assert mock_runner.run_cycle.await_args.kwargs["symbols"] == ["BTC/USD"]
