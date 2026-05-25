"""ROB-317 — operator CLI for the Binance Demo WebSocket scalping daemon.

Default-disabled. Behaviour is entirely env-gated (see WsDaemonGates):

* ``BINANCE_DEMO_SCALPING_ENABLED`` + ``BINANCE_DEMO_SCALPING_WS_ENABLED`` —
  both must be truthy for the daemon to subscribe/evaluate.
* ``BINANCE_DEMO_SCALPING_WS_CONFIRM`` — only when also truthy may real Demo
  orders be placed (slice 4 wires the bridge; this slice never mutates).

Slice 2 ships the gate plumbing only: with gates off it prints a disabled
summary and exits 0 without subscribing; with gates on it reports
``pending_supervisor`` (the streaming supervisor lands in slice 3) and still
does not open any socket. Demo hosts only; no live/testnet path; no secrets
printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates


def build_summary(gates: WsDaemonGates) -> dict[str, Any]:
    """Map resolved gates to a single-line JSON-able summary.

    ``subscribed`` is always False in slice 2 — no socket is ever opened here.
    """
    if not gates.daemon_active:
        return {
            "status": "disabled",
            "base_enabled": gates.base_enabled,
            "ws_enabled": gates.ws_enabled,
            "subscribed": False,
        }
    return {
        "status": "pending_supervisor",
        "base_enabled": gates.base_enabled,
        "ws_enabled": gates.ws_enabled,
        "mutation_allowed": gates.mutation_allowed,
        "subscribed": False,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-317 Binance Demo WebSocket scalping daemon. Default-disabled "
            "(zero side effects). Set BINANCE_DEMO_SCALPING_ENABLED=true and "
            "BINANCE_DEMO_SCALPING_WS_ENABLED=true to activate."
        )
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    gates = WsDaemonGates.from_env()
    summary = build_summary(gates)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
