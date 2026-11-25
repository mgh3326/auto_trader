"""hk 6503: the flag gate is wired into the screener entrypoint and analyze path."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_screener_entrypoint_calls_the_trading_flag_gate_after_halt_filter() -> None:
    from app.mcp_server.tooling.screening import entrypoint

    assert entrypoint.exclude_trading_flag_rows.__module__.endswith(
        "trading_flag_filter"
    )
    source = open(entrypoint.__file__, encoding="utf-8").read()
    halt = source.index("await exclude_halt_suspect_rows(")
    flags = source.index("await exclude_trading_flag_rows(")
    assert halt < flags


def test_analyze_path_reads_confirmed_halt_only_for_kr() -> None:
    from app.mcp_server.tooling import analysis_analyze

    source = open(analysis_analyze.__file__, encoding="utf-8").read()
    assert 'market_type == "equity_kr"' in source
    assert "confirmed_halt_flag" in source
