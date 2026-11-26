"""CLI entry-point contract for the manual KR market daily collector."""

from __future__ import annotations

import pytest

from app.core import cli
from scripts import collect_kr_market_daily

pytestmark = pytest.mark.unit


def test_main_configures_named_service_before_running(monkeypatch) -> None:
    configured: list[str] = []
    monkeypatch.setattr(
        collect_kr_market_daily,
        "parse_args",
        lambda: object(),
    )
    monkeypatch.setattr(
        cli,
        "setup_logging_and_sentry",
        lambda *, service_name: configured.append(service_name),
    )
    monkeypatch.setattr(
        collect_kr_market_daily.asyncio,
        "run",
        lambda coroutine: (coroutine.close(), 0)[1],
    )

    assert collect_kr_market_daily.main() == 0
    assert configured == ["collect_kr_market_daily"]
