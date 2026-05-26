"""ROB-317 — WS daemon gate config (default-disabled)."""

from __future__ import annotations

from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates


def test_empty_env_is_fully_disabled() -> None:
    gates = WsDaemonGates.from_env({})
    assert gates.base_enabled is False
    assert gates.ws_enabled is False
    assert gates.ws_confirm is False
    assert gates.daemon_active is False
    assert gates.mutation_allowed is False


def test_daemon_active_requires_both_base_and_ws() -> None:
    assert (
        WsDaemonGates.from_env({"BINANCE_DEMO_SCALPING_ENABLED": "true"}).daemon_active
        is False
    )
    assert (
        WsDaemonGates.from_env(
            {"BINANCE_DEMO_SCALPING_WS_ENABLED": "true"}
        ).daemon_active
        is False
    )
    assert (
        WsDaemonGates.from_env(
            {
                "BINANCE_DEMO_SCALPING_ENABLED": "true",
                "BINANCE_DEMO_SCALPING_WS_ENABLED": "true",
            }
        ).daemon_active
        is True
    )


def test_confirm_alone_never_enables_mutation() -> None:
    # Confirm true but ws_enabled false -> no mutation.
    gates = WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_CONFIRM": "true",
        }
    )
    assert gates.mutation_allowed is False


def test_mutation_allowed_requires_all_three() -> None:
    gates = WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_CONFIRM": "true",
        }
    )
    assert gates.mutation_allowed is True


def test_does_not_read_scheduler_confirm_flag() -> None:
    # The scheduler's confirm flag must not enable the daemon's mutation.
    gates = WsDaemonGates.from_env(
        {
            "BINANCE_DEMO_SCALPING_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_WS_ENABLED": "true",
            "BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM": "true",
        }
    )
    assert gates.ws_confirm is False
    assert gates.mutation_allowed is False
