"""ROB-273 — external-origin → SourceKind literal mapping."""

from __future__ import annotations

import pytest

from app.services.action_report.common.source_kind_mapping import (
    ALLOWED_SOURCE_KINDS,
    UnsupportedSourceKindError,
    map_source_kind,
)


def test_canonical_literals_pass_through():
    for literal in ALLOWED_SOURCE_KINDS:
        assert map_source_kind(literal) == literal


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("upbit_mcp", "auto_trader_mcp"),
        ("upbit_public", "auto_trader_mcp"),
        ("db", "auto_trader_mcp"),
        ("mcp_screen", "auto_trader_mcp"),
        ("auto_trader_db", "auto_trader_mcp"),
        ("domain_db", "domain_ref"),
        ("kis_api", "kis_mcp"),
        ("finnhub_crypto", "auto_trader_mcp"),
        ("finnhub", "auto_trader_mcp"),
        ("dart", "auto_trader_mcp"),
        ("news", "news_ingestor"),
        ("research_reports", "news_ingestor"),
        ("invest_http", "invest_api"),
        ("not_applicable", "manual"),
        ("operator_paste", "manual"),
    ],
)
def test_known_aliases_map_to_canonical(origin: str, expected: str) -> None:
    assert map_source_kind(origin) == expected
    assert expected in ALLOWED_SOURCE_KINDS


def test_unknown_origin_raises():
    with pytest.raises(UnsupportedSourceKindError):
        map_source_kind("totally_made_up_kind")


@pytest.mark.parametrize("bad", ["", None, 0, []])
def test_non_string_or_empty_rejected(bad: object) -> None:
    with pytest.raises(UnsupportedSourceKindError):
        map_source_kind(bad)  # type: ignore[arg-type]


def test_upbit_remote_debug_alias_not_invented():
    # The Linear ticket mentioned ``upbit_remote_debug`` but this PR
    # intentionally does NOT extend the SourceKind literal — the helper
    # must therefore reject the alias rather than smuggle in a new value.
    with pytest.raises(UnsupportedSourceKindError):
        map_source_kind("upbit_remote_debug")
