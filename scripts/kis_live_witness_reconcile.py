"""Manual-only missing-echo reconciler for the KIS live shadow witness."""

from __future__ import annotations

import asyncio

from app.services.brokers.kis.live_shadow_witness import (
    WitnessReconcileError,
    fetch_missing_echoes,
)


def main() -> int:
    try:
        witnesses = asyncio.run(fetch_missing_echoes())
    except WitnessReconcileError as exc:
        print(f"kis live witness reconcile failed: {exc}")
        return 2
    print(f"missing_echo_count={len(witnesses)}")
    for witness in witnesses:
        print(witness)
    return 1 if witnesses else 0


if __name__ == "__main__":
    raise SystemExit(main())
