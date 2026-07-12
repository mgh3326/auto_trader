"""ROB-841 review — real handler wiring integration tests.

These drive the actual ``_build_conditions_and_run`` path (only the low-level
Demo reader / signed client / DB session / executor are faked), rather than
stubbing ``_dry_run_preflight`` / ``_execute_confirmed_round_trip`` wholesale.
They prove:

* a provider failure fails closed BEFORE the signed client or DB session is
  ever constructed (both dry-run and confirm), and
* the *server-derived* ``MarketConditions`` instance produced by the real
  builder is handed to the executor unchanged (both dry-run and confirm).
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

import app.core.db as dbmod
import app.mcp_server.tooling.binance_demo_scalping_handler as mod
import app.services.brokers.binance.demo_scalping.market_data as mdmod
import app.services.brokers.binance.demo_scalping_exec.executor as exmod
import app.services.brokers.binance.demo_scalping_exec.reference as refmod
from app.services.brokers.binance.demo_scalping.contract import MarketConditions
from app.services.brokers.binance.demo_scalping.market_data import (
    BookTicker,
    spread_bps,
)
from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)


# --- low-level fakes ---------------------------------------------------------
class _RaisingReader:
    async def fetch_book_ticker(self, product, symbol):
        raise RuntimeError("bookTicker HTTP 503")

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        raise RuntimeError("klines HTTP 503")

    async def aclose(self):
        return None


class _ValidReader:
    async def fetch_book_ticker(self, product, symbol):
        return BookTicker(bid=Decimal("100"), ask=Decimal("100.05"))

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        now_ms = int(time.time() * 1000)
        return [
            Candle(
                open_time_ms=now_ms - 1000,  # ~1s old → fresh
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                close_time_ms=now_ms + 59_999,
            )
        ]

    async def aclose(self):
        return None


class _HarmlessRef:
    async def aclose(self):
        return None


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        return None


class _FakeClient:
    async def aclose(self):
        return None


class _Flag:
    def __init__(self):
        self.count = 0


def _from_env_recorder(flag: _Flag):
    def _fn(cls):
        flag.count += 1
        raise AssertionError("signed client from_env must not run on provider failure")

    return classmethod(_fn)


def _session_recorder(flag: _Flag):
    def _factory():
        flag.count += 1
        raise AssertionError("AsyncSessionLocal must not run on provider failure")

    return _factory


def _dry_result():
    return type(
        "R",
        (),
        {
            "status": "dry_run",
            "reason_codes": (),
            "sized_qty": Decimal("1"),
            "sized_notional_usdt": Decimal("10"),
        },
    )()


def _confirm_result():
    return type(
        "R",
        (),
        {
            "status": "reconciled",
            "open_client_order_id": "rob307-x",
            "close_client_order_id": "rob307-y",
            "exit_reason": "take_profit",
            "to_evidence_dict": lambda self: {"status": "reconciled"},
        },
    )()


def _capturing_executor(store: dict):
    class _Exec:
        def __init__(self, **kwargs):
            store["ctor"] = kwargs

        async def execute_monitored(self, intent, *, confirm, market, **kwargs):
            store["market"] = market
            store["confirm"] = confirm
            return _confirm_result() if confirm else _dry_result()

    return _Exec


# --- provider-failure ordering (before signed client / DB session) -----------
@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run,confirm", [(True, False), (False, True)])
async def test_provider_failure_blocks_before_client_and_session(
    monkeypatch, dry_run, confirm
) -> None:
    from_env_flag = _Flag()
    session_flag = _Flag()
    monkeypatch.setattr(mdmod, "DemoScalpingMarketData", lambda **k: _RaisingReader())
    monkeypatch.setattr(refmod, "DemoReferenceData", lambda **k: _HarmlessRef())
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", _session_recorder(session_flag))
    monkeypatch.setattr(
        BinanceFuturesDemoExecutionClient, "from_env", _from_env_recorder(from_env_flag)
    )
    # If the executor were somehow reached, this would explode too.
    monkeypatch.setattr(
        exmod,
        "DemoScalpingExecutor",
        _capturing_executor({}),
    )

    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT",
        side="BUY",
        rationale="funding flip",
        dry_run=dry_run,
        confirm=confirm,
    )
    assert result["status"] == "market_conditions_unavailable"
    assert result["dry_run"] is dry_run
    assert "provider_error" in result["reason"]
    assert from_env_flag.count == 0  # signed client never constructed
    assert session_flag.count == 0  # DB session never opened


# --- server-derived MarketConditions instance flows to the executor ----------
@pytest.mark.asyncio
async def test_dry_run_real_build_hands_server_market_to_executor(monkeypatch) -> None:
    store: dict = {}
    monkeypatch.setattr(mdmod, "DemoScalpingMarketData", lambda **k: _ValidReader())
    monkeypatch.setattr(refmod, "DemoReferenceData", lambda **k: _HarmlessRef())
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(exmod, "DemoScalpingExecutor", _capturing_executor(store))

    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="x", dry_run=True
    )
    assert result["status"] == "planned"
    assert store["confirm"] is False
    market = store["market"]
    assert isinstance(market, MarketConditions)
    # Spread is the server-observed value, computed from the reader's quote.
    assert market.spread_bps == spread_bps(
        BookTicker(bid=Decimal("100"), ask=Decimal("100.05"))
    )
    assert 0.0 <= market.data_age_seconds < 120.0


@pytest.mark.asyncio
async def test_confirm_real_build_hands_server_market_to_executor(monkeypatch) -> None:
    store: dict = {}
    monkeypatch.setattr(mdmod, "DemoScalpingMarketData", lambda **k: _ValidReader())
    monkeypatch.setattr(refmod, "DemoReferenceData", lambda **k: _HarmlessRef())
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        BinanceFuturesDemoExecutionClient,
        "from_env",
        classmethod(lambda cls: _FakeClient()),
    )
    monkeypatch.setattr(exmod, "DemoScalpingExecutor", _capturing_executor(store))

    result = await mod.binance_demo_scalping_submit_decision(
        symbol="SOLUSDT",
        side="SELL",
        rationale="OI fade",
        dry_run=False,
        confirm=True,
    )
    assert result["status"] == "reconciled"
    assert store["confirm"] is True
    market = store["market"]
    assert isinstance(market, MarketConditions)
    assert market.spread_bps == spread_bps(
        BookTicker(bid=Decimal("100"), ask=Decimal("100.05"))
    )
