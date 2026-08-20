"""D2 one-shot remediation CLI — the operator entry point for the writer.

Default-disabled, dry-run by default, and bound to the sealed r7 attempt-2
snapshot.  It can express exactly three orders and no fourth:

    BTCUSDT  SELL LIMIT 0.00015000 @ 69266.01000000
    ETHUSDT  SELL LIMIT 0.00520000 @  2248.56000000
    USDCUSDT SELL LIMIT 5000.00000000 @ 1.00072000

There is no ``--symbol``, ``--side``, ``--quantity``, or ``--price`` flag,
because there is nothing for them to select.  The order set comes from the
sealed payload, is checked against the frozen constant in
``app.services.brokers.binance.spot_demo.d2_remediation_single``, and any
disagreement is a refusal.

**Relationship to ``scripts/binance_spot_demo_smoke.py``.**  That CLI is the
ROB-298 BUY round-trip smoke path and remains exactly what it is; it does not
wire a SELL ``--confirm``, so it cannot express these operations.  This file is
the second — and last — approved Spot Demo entry point that places real Demo
orders.  Both CLIs name each other so neither can be read as the sole one.

Modes (mutually exclusive; the default prints guidance and exits 0):

  1. **default** — no flags: one guidance line, exit 0, zero HTTP / DB / lease.
  2. ``--plan-only`` — bind the seal and print the three request payloads.
     Pure: no HTTP, no DB, no lease, no signing.
  3. ``--dry-run`` — the whole path, stopping immediately before the signed
     POST: env gates, host re-assertion, seal binding, J3A lease acquisition
     and attestation, request composition, and the non-mutating
     ``POST /api/v3/order/test`` shape check.
  4. ``--confirm`` — the real dispatch.  Requires ``--dry-run`` to have been
     run first is *not* enforceable from here, so this mode instead requires
     both env gates plus the explicit flag, and it is the only mode that can
     reach ``submit_order(..., confirm=True)``.

Env gates (both default-off; neither relaxes the other):
  * ``BINANCE_SPOT_DEMO_ENABLED`` — ROB-298's existing client gate.
  * ``D2_REMEDIATION_SINGLE_ENABLED`` — this one-shot's dedicated gate.

Exit codes:
  0 — clean run (including the default-disabled exit).
  1 — operator misconfiguration (gate off, missing payload, seal mismatch).
  2 — runtime failure (lease unavailable, broker error, anomaly, halted run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_ALLOWED_OPERATION_IDS,
    D2_BOUND_ORDERS,
    D2_PRE_SNAPSHOT_HASH,
    D2_REMEDIATION_ENABLED_ENV,
    D2_REMEDIATION_ID,
    WRITER_NAME,
    D2DryRunReport,
    D2ExecutionReport,
    D2ReasonCode,
    D2RemediationError,
    D2RemediationSingleWriter,
    D2SealBindingMismatch,
    acquire_d2_lease,
    bind_sealed_orders,
    d2_remediation_enabled,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

logger = logging.getLogger("binance_spot_demo_d2_remediation")

_SPOT_DEMO_ENABLED_ENV = "BINANCE_SPOT_DEMO_ENABLED"


def _emit(payload: dict[str, Any]) -> None:
    """One JSON object per run, on stdout, for the evidence file."""

    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def load_sealed_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED,
            f"{path}: sealed payload is not a JSON object",
        )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="binance_spot_demo_d2_remediation",
        description=(
            "D2 one-shot remediation writer (d2_remediation_single). "
            "Default-disabled; dry-run by default."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan-only",
        action="store_true",
        help="print the three bound requests; no HTTP, no DB, no lease",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="run the whole path and stop immediately before the signed POST",
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="dispatch the three authorized SELL LIMIT orders for real",
    )
    parser.add_argument(
        "--sealed-payload",
        type=Path,
        help="path to the sealed r7 attempt-2 binding payload JSON",
    )
    parser.add_argument(
        "--no-order-test",
        action="store_true",
        help="skip the non-mutating POST /api/v3/order/test shape check",
    )
    return parser


def _gates_armed() -> tuple[bool, list[str]]:
    missing: list[str] = []
    if os.environ.get(_SPOT_DEMO_ENABLED_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        missing.append(_SPOT_DEMO_ENABLED_ENV)
    if not d2_remediation_enabled():
        missing.append(D2_REMEDIATION_ENABLED_ENV)
    return (not missing), missing


async def _run(args: argparse.Namespace) -> int:
    if not (args.plan_only or args.dry_run or args.confirm):
        _emit(
            {
                "writer": WRITER_NAME,
                "status": "NO_MODE_SELECTED",
                "detail": (
                    "pass --plan-only, --dry-run, or --confirm. Nothing was "
                    "read, locked, or dispatched."
                ),
                "broker_mutation_count": 0,
            }
        )
        return 0

    if args.sealed_payload is None:
        logger.error("--sealed-payload is required for every mode")
        return 1
    payload = load_sealed_payload(args.sealed_payload)

    if args.plan_only:
        # Pure: bind and print. No client, no lease, no socket, no DB.
        bind_sealed_orders(payload)
        _emit(
            {
                "schema_version": "d2-remediation-single-plan.v1",
                "writer": WRITER_NAME,
                "remediation_id": D2_REMEDIATION_ID,
                "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
                "sealed_payload_path": str(args.sealed_payload),
                "broker_mutation_count": 0,
                "operations": [
                    {
                        "operation_id": operation_id,
                        "request_params": order.request_params(),
                    }
                    for order, operation_id in zip(
                        D2_BOUND_ORDERS, D2_ALLOWED_OPERATION_IDS, strict=True
                    )
                ],
            }
        )
        return 0

    armed, missing = _gates_armed()
    if not armed:
        logger.error(
            "default-disabled: %s not armed; nothing was read, locked, or dispatched",
            ", ".join(missing),
        )
        return 1

    from app.core.db import _get_engine  # local: avoids an import-time DBAPI load

    execution = BinanceSpotDemoExecutionClient.from_env()
    lease = await acquire_d2_lease(engine=_get_engine())
    try:
        writer = D2RemediationSingleWriter(
            execution_client=execution,
            sealed_payload=payload,
            lease=lease,
            lease_grant=lease.grant,
        )
        report = await writer.execute(
            confirm=bool(args.confirm),
            include_order_test=not args.no_order_test,
        )
    finally:
        await execution.aclose()

    if isinstance(report, D2DryRunReport):
        _emit(report.as_evidence())
        return 0
    assert isinstance(report, D2ExecutionReport)
    _emit(report.as_evidence())
    return 2 if report.halted_reason else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except D2RemediationError as exc:
        logger.error("refused: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.error("runtime failure: %r", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
