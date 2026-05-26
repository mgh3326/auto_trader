"""ROB-323 — operator CLI for the Naver remote-debug data-quality audit.

Default-disabled. Connects ONLY to a local Chrome at 127.0.0.1:9222 launched
with the operator's logged-in profile:

    open -na "Google Chrome" --args \
      --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
      --user-data-dir="$HOME/.hermes/chrome-toss-debug"

Read-only: prints a JSON audit to stdout, never writes to the DB or any broker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from app.core.config import validate_remote_debug_audit_config
from app.core.db import AsyncSessionLocal  # sessionmaker(class_=AsyncSession)
from app.services.action_report.remote_debug_audit.cdp_client import CdpClient
from app.services.action_report.remote_debug_audit.service import (
    RemoteDebugAuditService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def run_preflight() -> dict[str, Any]:
    missing = validate_remote_debug_audit_config()
    return {"step": "preflight", "ok": not missing, "missing_env_keys": missing}


def require_target(args: argparse.Namespace) -> tuple[str, uuid.UUID]:
    if args.bundle_uuid:
        return "bundle", uuid.UUID(args.bundle_uuid)
    if args.report_uuid:
        return "report", uuid.UUID(args.report_uuid)
    raise ValueError("audit mode requires --bundle-uuid or --report-uuid")


def build_smoke_output(
    kind: str, target: uuid.UUID, bundle_uuid: uuid.UUID, audit: dict[str, Any]
) -> dict[str, Any]:
    """Operator envelope: surface the resolved target + key counts above the
    full audit payload so the runbook acceptance check is readable at a glance.
    """
    return {
        "step": "audit",
        "target_kind": kind,
        "report_uuid": str(target) if kind == "report" else None,
        "bundle_uuid": str(bundle_uuid),
        "checked_symbols": audit.get("checked_symbols", 0),
        "symbols_resolved": audit.get("symbols_resolved", 0),
        "audit": audit,
    }


def audit_exit_code(audit: dict[str, Any]) -> int:
    """0 when at least one symbol resolved on Naver; 3 otherwise.

    A non-zero audit exit is operator-actionable (env/config or external page
    change), NOT a report-generation failure — generation never calls this.
    """
    checked = audit.get("checked_symbols", 0)
    resolved = audit.get("symbols_resolved", 0)
    return 0 if checked > 0 and resolved > 0 else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Naver remote-debug data-quality audit (ROB-323, operator-only)"
    )
    parser.add_argument("--mode", required=True, choices=["preflight", "audit"])
    parser.add_argument("--bundle-uuid", default=None)
    parser.add_argument("--report-uuid", default=None)
    parser.add_argument("--max-symbols", type=int, default=10)
    return parser


async def _amain(args: argparse.Namespace) -> int:
    if args.mode == "preflight":
        _emit(run_preflight())
        return 0

    # audit mode
    missing = validate_remote_debug_audit_config()
    if missing:
        _emit({"step": "audit", "ok": False, "missing_env_keys": missing})
        return 2

    kind, target = require_target(args)
    async with AsyncSessionLocal() as session:
        svc = RemoteDebugAuditService(
            snapshots_repo=InvestmentSnapshotsRepository(session),
            reports_repo=InvestmentReportsRepository(session),
            cdp_session=CdpClient(),
        )
        bundle_uuid = (
            await svc.resolve_bundle_uuid(target) if kind == "report" else target
        )
        audit = await svc.audit_bundle(bundle_uuid, max_symbols=args.max_symbols)
    _emit(build_smoke_output(kind, target, bundle_uuid, audit))
    return audit_exit_code(audit)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
