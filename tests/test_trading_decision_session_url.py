from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.unit
def test_builds_url_from_base_and_uuid():
    from app.services.trading_decision_session_url import (
        build_trading_decision_session_url,
    )

    url = build_trading_decision_session_url(
        "https://trader.robinco.dev/", UUID("11111111-1111-1111-1111-111111111111")
    )
    assert url == (
        "https://trader.robinco.dev/trading/decisions/"
        "11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.unit
def test_strips_trailing_slashes_and_quotes_uuid():
    from app.services.trading_decision_session_url import (
        build_trading_decision_session_url,
    )

    url = build_trading_decision_session_url(
        "https://trader.robinco.dev///",
        UUID("22222222-2222-2222-2222-222222222222"),
    )
    assert url == (
        "https://trader.robinco.dev/trading/decisions/"
        "22222222-2222-2222-2222-222222222222"
    )


@pytest.mark.unit
def test_resolve_uses_configured_when_present():
    from app.services.trading_decision_session_url import (
        resolve_trading_decision_base_url,
    )

    resolved = resolve_trading_decision_base_url(
        configured="https://trader.robinco.dev",
        request_base_url="http://localhost:8000/",
    )
    assert resolved == "https://trader.robinco.dev"


@pytest.mark.unit
def test_resolve_falls_back_when_configured_blank():
    from app.services.trading_decision_session_url import (
        resolve_trading_decision_base_url,
    )

    resolved = resolve_trading_decision_base_url(
        configured="   ",
        request_base_url="http://localhost:8000/",
    )
    assert resolved == "http://localhost:8000/"


@pytest.mark.unit
def test_resolve_strips_configured_whitespace():
    from app.services.trading_decision_session_url import (
        resolve_trading_decision_base_url,
    )

    resolved = resolve_trading_decision_base_url(
        configured="  https://trader.robinco.dev/  ",
        request_base_url="http://localhost:8000/",
    )
    assert resolved == "https://trader.robinco.dev/"
