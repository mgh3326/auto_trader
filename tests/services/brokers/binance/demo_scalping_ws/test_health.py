"""ROB-317 — daemon health snapshot JSON."""

from __future__ import annotations

import datetime as dt
import json

from app.services.brokers.binance.demo_scalping_ws.health import (
    DaemonHealthSnapshot,
    SymbolHealth,
)

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def test_snapshot_serializes_to_json() -> None:
    snap = DaemonHealthSnapshot(
        generated_at=_NOW,
        connected=True,
        daemon_active=True,
        mutation_allowed=False,
        symbols=(
            SymbolHealth(
                symbol="XRPUSDT",
                fresh=True,
                last_event_at=_NOW - dt.timedelta(seconds=2),
                age_seconds=2.0,
            ),
        ),
    )
    payload = json.loads(snap.to_json())
    assert payload["connected"] is True
    assert payload["mutation_allowed"] is False
    assert payload["symbols"][0]["symbol"] == "XRPUSDT"
    assert payload["symbols"][0]["fresh"] is True
    assert payload["symbols"][0]["age_seconds"] == 2.0


def test_snapshot_handles_symbol_with_no_events() -> None:
    snap = DaemonHealthSnapshot(
        generated_at=_NOW,
        connected=False,
        daemon_active=False,
        mutation_allowed=False,
        symbols=(
            SymbolHealth(
                symbol="DOGEUSDT", fresh=False, last_event_at=None, age_seconds=None
            ),
        ),
    )
    payload = json.loads(snap.to_json())
    assert payload["symbols"][0]["last_event_at"] is None
    assert payload["symbols"][0]["age_seconds"] is None
