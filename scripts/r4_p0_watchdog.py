"""Independent availability/finalization watchdog for R4 P0 collectors.

The watchdog is intentionally a manual operator entrypoint.  This module does
not register cron, TaskIQ, launchd, or any other scheduler.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.services.brokers.binance.r4_p0_collector import (
    REQUIRED_ACTIVE_SOURCES,
    SIGNAL_SYMBOLS,
    AppendOnlyPITStore,
    runtime_code_hash,
    utc_now,
)
from app.services.brokers.binance.r4_p0_hardening import (
    EPOCH_HOURS,
    AlertDispatcher,
    EpochLedger,
    EpochPolicy,
    availability_report,
    finalization_report,
    floor_epoch,
    iso_utc,
    latest_heartbeat,
    latest_process_version,
)

ENABLED_ENV = "R4_P0_WATCHDOG_ENABLED"
ALERT_WEBHOOKS_ENV = "R4_P0_ALERT_WEBHOOK_URLS"
DEFAULT_STATE_ROOT = "~/work/herdr-artifacts/r4-p0-watchdog"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run",
        action="store_true",
        help=f"run until stopped; requires {ENABLED_ENV}=true",
    )
    mode.add_argument(
        "--t0-preflight",
        action="store_true",
        help="run one read-only preflight of the precommitted T0 and exit",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="collector SQLite artifact path; repeat for each independent replica",
    )
    parser.add_argument(
        "--state-root",
        default=DEFAULT_STATE_ROOT,
        help="local append-only watchdog alert ledger",
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--stale-after-seconds", type=float, default=120.0)
    parser.add_argument("--finalizer-grace-seconds", type=float, default=30.0)
    parser.add_argument("--minimum-healthy-replicas", type=int, default=2)
    parser.add_argument(
        "--allow-log-only-alerts",
        action="store_true",
        help="explicitly allow no HTTPS webhook during manual validation",
    )
    return parser.parse_args(argv)


def _alert_webhook_urls() -> tuple[str, ...]:
    raw = os.getenv(ALERT_WEBHOOKS_ENV, "")
    return tuple(
        item.strip()
        for chunk in raw.splitlines()
        for item in chunk.split(",")
        if item.strip()
    )


def _policy() -> EpochPolicy:
    return EpochPolicy(
        required_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        symbols=tuple(sorted(SIGNAL_SYMBOLS)),
    )


def t0_preflight_report(
    artifact_paths: Sequence[Path],
    policy: EpochPolicy,
    *,
    verified_at: dt.datetime,
    expected_code_hash: str,
    stale_after_seconds: float,
) -> dict[str, Any]:
    """Read collector artifacts once, without creating any local state."""

    paths = tuple(
        sorted({path.expanduser().resolve() for path in artifact_paths}, key=str)
    )
    availability = availability_report(
        paths,
        policy,
        observed_at=verified_at,
        stale_after_seconds=stale_after_seconds,
        expected_code_hash=expected_code_hash,
    )
    availability_by_path = {
        replica["artifact"]: replica for replica in availability["replicas"]
    }
    replicas: list[dict[str, Any]] = []
    for path in paths:
        heartbeat = latest_heartbeat(path, policy)
        version = (
            latest_process_version(path, policy, run_id=heartbeat["run_id"])
            if heartbeat is not None
            else None
        )
        availability_replica = availability_by_path[str(path)]
        replicas.append(
            {
                "artifact": str(path),
                "status": availability_replica["status"],
                "collector_instance_id": (
                    heartbeat["collector_instance_id"]
                    if heartbeat is not None
                    else None
                ),
                "code_hash": version["code_hash"] if version is not None else None,
                "stamped_t0_utc": (
                    version.get("t0_utc") if version is not None else None
                ),
                "heartbeat_observed_at": (
                    heartbeat["observed_at"] if heartbeat is not None else None
                ),
                "age_seconds": availability_replica.get("age_seconds"),
            }
        )

    configured_t0 = iso_utc(policy.t0)
    t0_minus_4h = policy.t0 - dt.timedelta(hours=EPOCH_HOURS)
    gates = {
        "v_le_t0_minus_4h": verified_at <= t0_minus_4h,
        "code_hash_match_all": len(replicas) >= 2
        and all(replica["code_hash"] == expected_code_hash for replica in replicas),
        "healthy_replica_count_ge_2": availability["healthy_replica_count"] >= 2,
        "stamped_t0_matches_all": len(replicas) >= 2
        and all(replica["stamped_t0_utc"] == configured_t0 for replica in replicas),
    }
    ok = all(gates.values())
    return {
        "t0_utc": configured_t0,
        "t0_minus_4h": iso_utc(t0_minus_4h),
        "verified_at": iso_utc(verified_at),
        "expected_code_hash": expected_code_hash,
        "replicas": replicas,
        "gates": gates,
        "ok": ok,
        "summary": (
            "PASS: committed T0 preflight gates are satisfied"
            if ok
            else "FAIL: one or more committed T0 preflight gates are closed"
        ),
        "next_action": (
            "고정된 T0 로 진행"
            if ok
            else "기존 T0 를 소급 변경하지 말고 새 커밋과 새 T0 를 사전 고정하라"
        ),
    }


async def _watch(args: argparse.Namespace) -> int:
    policy = _policy()
    expected_code_hash = runtime_code_hash()
    artifact_paths = tuple(Path(path) for path in args.artifact)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    with AppendOnlyPITStore(
        Path(args.state_root),
        artifact_filename="r4_p0_watchdog.sqlite3",
        lock_filename=".watchdog.lock",
    ) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001
        dispatcher = AlertDispatcher(ledger, _alert_webhook_urls())
        while not stop.is_set():
            now = utc_now()
            availability = availability_report(
                artifact_paths,
                policy,
                observed_at=now,
                stale_after_seconds=args.stale_after_seconds,
                expected_code_hash=expected_code_hash,
            )
            if not availability["version_stamp_match"]:
                await dispatcher.emit(
                    alert_key=(
                        "COLLECTOR_VERSION_MISMATCH:"
                        f"{policy.study_id}:{policy.policy_hash}:"
                        f"{int(now.timestamp()) // 900}"
                    ),
                    severity="CRITICAL",
                    payload={
                        "alert_type": "COLLECTOR_VERSION_MISMATCH",
                        **availability,
                    },
                    now=now,
                )
            if availability["healthy_replica_count"] < args.minimum_healthy_replicas:
                await dispatcher.emit(
                    alert_key=(
                        "COLLECTOR_REDUNDANCY_LOST:"
                        f"{policy.study_id}:{policy.policy_hash}:"
                        f"{int(now.timestamp()) // 900}"
                    ),
                    severity="CRITICAL",
                    payload={
                        "alert_type": "COLLECTOR_REDUNDANCY_LOST",
                        "minimum_healthy_replicas": (args.minimum_healthy_replicas),
                        **availability,
                    },
                    now=now,
                )

            latest_due = floor_epoch(now) - dt.timedelta(hours=4)
            due_at = latest_due + dt.timedelta(
                hours=4,
                seconds=args.finalizer_grace_seconds,
            )
            if latest_due >= policy.t0 and now >= due_at:
                report = finalization_report(
                    artifact_paths,
                    policy,
                    decision_epoch=latest_due,
                )
                if report["missing_symbols"] or report["divergent_symbols"]:
                    await dispatcher.emit(
                        alert_key=(
                            "FINALIZER_STALLED:"
                            f"{policy.study_id}:{policy.policy_hash}:"
                            f"{report['decision_epoch_utc']}"
                        ),
                        severity="CRITICAL",
                        payload={
                            "alert_type": "FINALIZER_STALLED",
                            "finalizer_grace_seconds": (args.finalizer_grace_seconds),
                            "policy_hash": policy.policy_hash,
                            "study_id": policy.study_id,
                            **report,
                        },
                        now=now,
                    )
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.interval_seconds)
            except TimeoutError:
                pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    formatter = logging.Formatter(fmt="%(asctime)sZ %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    artifacts = {str(Path(path).expanduser().resolve()) for path in args.artifact}
    if args.t0_preflight:
        if len(artifacts) < 2:
            raise SystemExit(
                "--t0-preflight requires at least two distinct collector artifacts"
            )
        if args.stale_after_seconds <= 0:
            raise SystemExit("--stale-after-seconds must be positive")
        report = t0_preflight_report(
            tuple(Path(path) for path in artifacts),
            _policy(),
            verified_at=utc_now(),
            expected_code_hash=runtime_code_hash(),
            stale_after_seconds=args.stale_after_seconds,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    payload = {
        "alert_webhook_count": len(_alert_webhook_urls()),
        "artifacts": [str(Path(path).expanduser()) for path in args.artifact],
        "broker_mutation": False,
        "database_write": bool(args.run),
        "expected_code_hash": runtime_code_hash(),
        "minimum_healthy_replicas": args.minimum_healthy_replicas,
        "mode": "run" if args.run else "dry_run",
        "network": bool(args.run and _alert_webhook_urls()),
        "scheduler_registration": False,
        "state_root": str(Path(args.state_root).expanduser()),
    }
    if not args.run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if os.getenv(ENABLED_ENV, "").strip().lower() != "true":
        raise SystemExit(f"refusing --run: set {ENABLED_ENV}=true explicitly")
    if args.minimum_healthy_replicas < 2 or len(artifacts) < 2:
        raise SystemExit("--run requires at least two distinct collector artifacts")
    if args.interval_seconds <= 0 or args.stale_after_seconds <= 0:
        raise SystemExit("watchdog intervals must be positive")
    if not _alert_webhook_urls() and not args.allow_log_only_alerts:
        raise SystemExit(
            f"--run requires {ALERT_WEBHOOKS_ENV}; "
            "use --allow-log-only-alerts only for explicit validation"
        )
    return asyncio.run(_watch(args))


if __name__ == "__main__":
    raise SystemExit(main())
