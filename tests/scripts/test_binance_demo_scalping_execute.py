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
