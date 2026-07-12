"""ROB-307 PR2 — tests for the one-shot executor CLI (no broker, no DB)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from scripts.binance_demo_scalping_execute import (
    _truthy,
    build_manual_intent,
    main,
)

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)


def test_truthy() -> None:
    assert _truthy("true") and _truthy("1")
    assert not _truthy(None) and not _truthy("off")


def test_disabled_by_default_returns_zero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_ENABLED", raising=False)
    rc = main(["--product", "spot", "--symbol", "XRPUSDT"])
    assert rc == 0
    assert "demo_scalping_execute" not in capsys.readouterr().out


def test_build_manual_intent_pins_notional_and_side() -> None:
    intent = build_manual_intent(
        product="usdm_futures",
        symbol="XRPUSDT",
        side="SELL",
        now=_NOW,
        limits=ScalpingRiskLimits(),
    )
    assert intent.product == "usdm_futures"
    assert intent.symbol == "XRPUSDT"
    assert intent.side == "SELL"
    assert intent.order_type == "MARKET"
    assert intent.target_notional_usdt == Decimal("10")
    assert intent.reason_codes == ("manual_executor",)


def test_disabled_path_constructs_no_client(monkeypatch) -> None:
    # With the feature gate off, no execution client / engine is touched.
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_ENABLED", raising=False)
    import httpx

    def _boom(*a, **k):
        raise AssertionError("disabled path must not construct an httpx client")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    assert main(["--product", "usdm_futures", "--symbol", "XRPUSDT"]) == 0


class _FakeClient:
    async def aclose(self) -> None:
        return None


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self) -> None:
        return None


class _FakeReference:
    def __init__(self, **kwargs) -> None:
        pass

    async def aclose(self) -> None:
        return None


def _canned_market_conditions():
    from app.services.brokers.binance.demo_scalping.contract import MarketConditions

    return MarketConditions(
        spread_bps=Decimal("2"),
        data_age_seconds=5.0,
        spot_free_base_qty=Decimal("0"),
    )


async def _fake_build_market_conditions(market_data, **kwargs):
    # ROB-841: the CLI now derives a server market snapshot before executing.
    # Stub it so the wiring tests stay hermetic (no network).
    return _canned_market_conditions()


class _FakeResult:
    def __init__(self, status: str = "dry_run") -> None:
        self.status = status

    def to_evidence_dict(self) -> dict:
        return {"status": self.status}


class _FakeExecutor:
    def __init__(self, **kwargs) -> None:
        pass

    async def execute(self, intent, *, confirm: bool, **kwargs):
        return _FakeResult("dry_run")

    async def execute_monitored(self, intent, *, confirm: bool, **kwargs):
        return _FakeResult("reconciled")


def _patch_wiring(monkeypatch) -> None:
    import app.core.db as dbmod
    import app.services.brokers.binance.demo_scalping.market_data as mdmod
    import app.services.brokers.binance.demo_scalping_exec.executor as exmod
    import app.services.brokers.binance.demo_scalping_exec.reference as refmod
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    for cls in (BinanceSpotDemoExecutionClient, BinanceFuturesDemoExecutionClient):
        monkeypatch.setattr(cls, "from_env", classmethod(lambda cls: _FakeClient()))
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(refmod, "DemoReferenceData", _FakeReference)
    monkeypatch.setattr(mdmod, "DemoScalpingMarketData", _FakeReference)
    monkeypatch.setattr(mdmod, "build_market_conditions", _fake_build_market_conditions)
    monkeypatch.setattr(exmod, "DemoScalpingExecutor", _FakeExecutor)


def test_enabled_dry_run_wiring_does_not_await_sync_from_env(
    monkeypatch, capsys
) -> None:
    # Regression guard: from_env is SYNC; the CLI must not await it. Drives the
    # full _run wiring with fakes (no creds, no DB, no network). If someone
    # re-adds `await ...from_env()`, awaiting _FakeClient() raises and rc != 0.
    import app.core.db as dbmod
    import app.services.brokers.binance.demo_scalping.market_data as mdmod
    import app.services.brokers.binance.demo_scalping_exec.executor as exmod
    import app.services.brokers.binance.demo_scalping_exec.reference as refmod
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.setattr(
        BinanceSpotDemoExecutionClient,
        "from_env",
        classmethod(lambda cls: _FakeClient()),
    )
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(refmod, "DemoReferenceData", _FakeReference)
    monkeypatch.setattr(mdmod, "DemoScalpingMarketData", _FakeReference)
    monkeypatch.setattr(mdmod, "build_market_conditions", _fake_build_market_conditions)
    monkeypatch.setattr(exmod, "DemoScalpingExecutor", _FakeExecutor)

    rc = main(["--product", "spot", "--symbol", "DOGEUSDT"])
    assert rc == 0
    assert "dry_run" in capsys.readouterr().out


def test_monitor_flag_routes_to_execute_monitored(monkeypatch, capsys) -> None:
    _patch_wiring(monkeypatch)
    rc = main(
        ["--product", "usdm_futures", "--symbol", "XRPUSDT", "--monitor", "--confirm"]
    )
    assert rc == 0  # reconciled (monitored, flat) -> success
    assert "reconciled" in capsys.readouterr().out


class _CloseFlag:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_market_conditions_unavailable_exits_blocked_no_side_effects(
    monkeypatch, capsys
) -> None:
    # ROB-841 CLI contract: an unavailable server market snapshot is a blocked
    # outcome (exit 1) with structured evidence — NOT a generic runtime failure
    # (exit 2). The DB session, executor, and broker submit are never reached,
    # and the constructed client/reference/market-data resources are closed.
    import json

    import app.core.db as dbmod
    import app.services.brokers.binance.demo_scalping.market_data as mdmod
    import app.services.brokers.binance.demo_scalping_exec.executor as exmod
    import app.services.brokers.binance.demo_scalping_exec.reference as refmod
    from app.services.brokers.binance.demo_scalping.market_data import (
        MarketConditionsUnavailable,
    )
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")

    client_flag = _CloseFlag()
    reference_flag = _CloseFlag()
    market_data_flag = _CloseFlag()

    def _forbidden_session():
        raise AssertionError("AsyncSessionLocal must not open on unavailable market")

    class _ForbiddenExecutor:
        def __init__(self, **kwargs):
            raise AssertionError(
                "executor must not be constructed on unavailable market"
            )

    async def _raise_build(market_data, **kwargs):
        raise MarketConditionsUnavailable("provider_error: RuntimeError: boom")

    monkeypatch.setattr(
        BinanceSpotDemoExecutionClient,
        "from_env",
        classmethod(lambda cls: client_flag),
    )
    monkeypatch.setattr(refmod, "DemoReferenceData", lambda **k: reference_flag)
    monkeypatch.setattr(mdmod, "DemoScalpingMarketData", lambda **k: market_data_flag)
    monkeypatch.setattr(mdmod, "build_market_conditions", _raise_build)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", _forbidden_session)
    monkeypatch.setattr(exmod, "DemoScalpingExecutor", _ForbiddenExecutor)

    rc = main(["--product", "spot", "--symbol", "XRPUSDT"])
    assert rc == 1  # blocked, not the generic runtime exit 2

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "demo_scalping_execute" in line
    ]
    assert lines, "expected an evidence line"
    payload = json.loads(lines[-1])
    assert payload["event"] == "demo_scalping_execute"
    assert payload["status"] == "blocked"
    assert "market_conditions_unavailable" in payload["reason_codes"]

    # Resources created before the failure are closed; nothing downstream ran.
    assert client_flag.closed is True
    assert reference_flag.closed is True
    assert market_data_flag.closed is True
