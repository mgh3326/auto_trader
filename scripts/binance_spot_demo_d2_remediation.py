"""D2 one-shot remediation CLI — the operator entry point for the writer.

Default-disabled, dry-run by default, and bound to the sealed r7 attempt-2
snapshot.  It can express exactly three orders and no fourth:

    BTCUSDT  SELL LIMIT 0.00015000    @ 69266.01000000
    ETHUSDT  SELL LIMIT 0.00520000    @  2248.56000000
    USDCUSDT SELL LIMIT 5000.00000000 @     1.00072000

There is no ``--symbol``, ``--side``, ``--quantity``, or ``--price`` flag,
because there is nothing for them to select.  The orders come from the sealed
payload file, whose **bytes are hashed and checked against a registered
digest** before the JSON is even parsed.

**``--confirm`` cannot dispatch today, and that is by construction.** Every
registered sealed payload carries ``dispatch_authorized=false``, the current r7
object has ``operator_authorization=null``, no expiry, and
``mutation_authorized=false`` on all three rows. ``--dry-run`` prints that list;
``--confirm`` refuses on it. Authorizing dispatch takes the operator's re-sign
(which produces different bytes, and therefore a different digest) plus a
reviewed change that registers it.

**Relationship to ``scripts/binance_spot_demo_smoke.py``.**  That CLI is the
ROB-298 BUY round-trip smoke path and remains exactly what it is; it does not
wire a SELL ``--confirm``, so it cannot express these operations.  Both CLIs
name each other so neither can be read as the sole one.

Modes (mutually exclusive; the default prints guidance and exits 0):

  1. **default** — no flags: one guidance line, exit 0, zero HTTP / DB / lease.
  2. ``--plan-only`` — verify the sealed file and print the three request
     payloads with their deterministic client-order-ids. No HTTP, no DB, no
     lease, no signing.
  3. ``--dry-run`` — the whole path, stopping immediately before the signed
     POST: both env gates, host re-assertion, credential-fingerprint match,
     writer freeze, seal verification, J3A lease acquisition and attestation,
     pre-dispatch account truth, request composition, and the non-mutating
     ``POST /api/v3/order/test`` shape check.
  4. ``--confirm`` — the real dispatch. Requires everything ``--dry-run`` does,
     plus complete dispatch authority. The order-shape check is not optional
     here; there is no flag to skip it.

Env gates (both default-off; neither relaxes the other, and both are read from
the real process environment):
  * ``BINANCE_SPOT_DEMO_ENABLED`` — ROB-298's existing client gate.
  * ``D2_REMEDIATION_SINGLE_ENABLED`` — this one-shot's dedicated gate.

Exit codes:
  0 — clean run (including the default-disabled exit).
  1 — operator misconfiguration (gate off, unregistered/mismatched seal,
      dispatch not authorized).
  2 — runtime failure (lease unavailable, account drift, broker error,
      anomaly, halted run, unreleased lease).
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
    D2RemediationError,
    D2RemediationSingleWriter,
    acquire_d2_lease,
    d2_remediation_enabled,
    load_sealed_authority,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

logger = logging.getLogger("binance_spot_demo_d2_remediation")

_SPOT_DEMO_ENABLED_ENV = "BINANCE_SPOT_DEMO_ENABLED"


def _emit(payload: dict[str, Any]) -> None:
    """One JSON object per run, on stdout, for the evidence file."""

    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


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
        help="verify the sealed file and print the three bound requests; "
        "no HTTP, no DB, no lease",
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
    # Hashes the file bytes and refuses an unregistered digest before parsing.
    authority = load_sealed_authority(args.sealed_payload)

    if args.plan_only:
        # Pure: verify and print. No client, no lease, no socket, no DB.
        _emit(
            {
                "schema_version": "d2-remediation-single-plan.v1",
                "writer": WRITER_NAME,
                "remediation_id": D2_REMEDIATION_ID,
                "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
                "authority": authority.as_evidence(),
                "dispatch_block_reasons": list(
                    authority.dispatch_block_reasons(now=_now())
                ),
                "broker_mutation_count": 0,
                "operations": [
                    {
                        "operation_id": operation_id,
                        "client_order_id": order.client_order_id,
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

    if args.confirm:
        # Refuse before opening a client, a lease, or a database session:
        # nothing about a broker connection improves an unsigned seal.
        blockers = authority.dispatch_block_reasons(now=_now())
        if blockers:
            logger.error(
                "dispatch not authorized under this seal; nothing was opened. "
                "Blockers: %s",
                "; ".join(blockers),
            )
            _emit(
                {
                    "writer": WRITER_NAME,
                    "status": "DISPATCH_NOT_AUTHORIZED",
                    "authority": authority.as_evidence(),
                    "dispatch_block_reasons": list(blockers),
                    "broker_mutation_count": 0,
                }
            )
            return 1

    # Local imports: both pull the asyncpg DBAPI, which the offline modes above
    # must not pay for.
    from app.core.db import AsyncSessionLocal, _get_engine
    from app.services.brokers.binance.demo.ledger.service import (
        BinanceDemoLedgerService,
    )

    execution = BinanceSpotDemoExecutionClient.from_env()
    lease = await acquire_d2_lease(engine=_get_engine())
    lease_release_evidence: dict[str, Any] = {}
    try:
        async with AsyncSessionLocal() as session:
            # The writer refuses a None ledger, so this is not optional wiring:
            # there is no dispatch path that leaves no durable evidence.
            ledger = BinanceDemoLedgerService(session)
            writer = D2RemediationSingleWriter(
                execution_client=execution,
                authority=authority,
                lease=lease,
                lease_grant=lease.grant,
                ledger=ledger,
            )
            report = await writer.execute(confirm=bool(args.confirm))
            await session.commit()
    finally:
        await execution.aclose()
        lease_release_evidence = await _release_lease(lease)

    body = report.as_evidence()
    body["lease_release"] = lease_release_evidence
    _emit(body)
    if not lease_release_evidence.get("released"):
        # A lease that cannot be proven released leaves the account coordinated
        # by a process that is exiting. That is fail-closed rather than
        # fail-open, but it still needs an operator, so it is not exit 0.
        return 2
    if isinstance(report, D2DryRunReport):
        return 0
    assert isinstance(report, D2ExecutionReport)
    return 2 if report.halted_reason else 0


def _now() -> Any:
    import datetime as dt

    return dt.datetime.now(dt.UTC)


async def _release_lease(lease: Any) -> dict[str, Any]:
    """Release the J3A lease and say plainly whether it was proven released.

    The lease outlives the execution client on purpose — it is still held
    across the proof epochs — so it is released here, last, and the outcome is
    reported rather than swallowed. An unreleased authority is recorded by the
    coordination layer as a hold; surfacing its id is what lets an operator
    find the stuck account instead of guessing.
    """

    try:
        await lease.release(lease.grant)
    except Exception as exc:
        hold = getattr(lease, "unreleased_authority_hold", None)
        return {
            "released": False,
            "error": repr(exc),
            "unreleased_authority_hold": None if hold is None else str(hold),
        }
    hold = getattr(lease, "unreleased_authority_hold", None)
    return {
        "released": bool(getattr(lease, "released", False)),
        "unlocked_keys": list(getattr(lease, "unlocked_keys", ()) or ()),
        "unreleased_authority_hold": None if hold is None else str(hold),
    }


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
