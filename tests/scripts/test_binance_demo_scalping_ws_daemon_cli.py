"""ROB-317 — WS daemon CLI default-disabled behavior."""

from __future__ import annotations

import json

from scripts.binance_demo_scalping_ws_daemon import build_summary, main
from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates


def test_summary_disabled_when_gates_off() -> None:
    gates = WsDaemonGates(base_enabled=False, ws_enabled=False, ws_confirm=False)
    summary = build_summary(gates)
    assert summary["status"] == "disabled"
    assert summary["subscribed"] is False


def test_summary_pending_supervisor_when_gates_on() -> None:
    gates = WsDaemonGates(base_enabled=True, ws_enabled=True, ws_confirm=False)
    summary = build_summary(gates)
    assert summary["status"] == "pending_supervisor"
    assert summary["subscribed"] is False
    assert summary["mutation_allowed"] is False


def test_main_disabled_exits_zero_and_prints_json(capsys, monkeypatch) -> None:
    for key in (
        "BINANCE_DEMO_SCALPING_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_CONFIRM",
    ):
        monkeypatch.delenv(key, raising=False)
    rc = main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "disabled"
    assert out["subscribed"] is False
