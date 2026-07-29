"""Manual R4 P0 Binance USD-M point-in-time collector.

Default invocation is a no-network dry-run.  Long-running collection requires
both ``R4_P0_COLLECTOR_ENABLED=true`` and ``--run``.  ``--probe`` is an
explicit, bounded (<=180 seconds) production-public validation mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from app.services.brokers.binance.r4_p0_collector import (
    COLLECTOR_VERSION,
    PIT_COLUMNS,
    REQUIRED_ACTIVE_SOURCES,
    REST_PATH_ALLOWLIST,
    SIGNAL_SYMBOLS,
    SYMBOLS,
    AppendOnlyPITStore,
    BinanceR4P0Collector,
    CollectorConfig,
    redact_sample,
    runtime_code_hash,
    utc_now,
)
from app.services.brokers.binance.r4_p0_hardening import (
    StudyManifest,
    load_study_manifest,
)

ENABLED_ENV = "R4_P0_COLLECTOR_ENABLED"
ARTIFACT_ENV = "R4_P0_ARTIFACT_ROOT"
COLLECTOR_ID_ENV = "R4_P0_COLLECTOR_ID"
ALERT_WEBHOOKS_ENV = "R4_P0_ALERT_WEBHOOK_URLS"
STUDY_MANIFEST_ENV = "R4_P0_STUDY_MANIFEST"
STUDY_MANIFEST_SHA256_ENV = "R4_P0_STUDY_MANIFEST_SHA256"
DEFAULT_ARTIFACT_ROOT = "~/work/herdr-artifacts/r4-p0-collector"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run", action="store_true", help="run until stopped (env-gated)"
    )
    mode.add_argument(
        "--probe",
        action="store_true",
        help="bounded public-network validation; does not require the operational env gate",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after N seconds; required for --probe and capped at 180",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.getenv(ARTIFACT_ENV, DEFAULT_ARTIFACT_ROOT),
    )
    parser.add_argument(
        "--study-manifest",
        default=os.getenv(STUDY_MANIFEST_ENV),
        help="absolute path to the sealed, effective-dated study manifest",
    )
    parser.add_argument(
        "--study-manifest-sha256",
        default=os.getenv(STUDY_MANIFEST_SHA256_ENV),
        help="externally supplied SHA-256 pin for the canonical manifest JSON",
    )
    parser.add_argument("--status-seconds", type=float, default=30.0)
    parser.add_argument(
        "--collector-id",
        default=os.getenv(COLLECTOR_ID_ENV, "r4-p0-local"),
        help="stable identity unique to this host/collector replica",
    )
    parser.add_argument(
        "--replica-artifact",
        action="append",
        default=[],
        help=(
            "read-only peer r4_p0_collector.sqlite3 path; repeat for "
            "independent replicas"
        ),
    )
    parser.add_argument(
        "--minimum-healthy-replicas",
        type=int,
        default=2,
        help="required fresh collector heartbeats (operational default: 2)",
    )
    parser.add_argument(
        "--allow-log-only-alerts",
        action="store_true",
        help="explicitly permit no HTTPS webhook (probe/manual validation only)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="offline integrity audit and one sample per source; no network",
    )
    return parser.parse_args()


def _enabled() -> bool:
    return os.getenv(ENABLED_ENV, "").strip().lower() == "true"


def _alert_webhook_urls() -> tuple[str, ...]:
    raw = os.getenv(ALERT_WEBHOOKS_ENV, "")
    return tuple(
        item.strip()
        for chunk in raw.splitlines()
        for item in chunk.split(",")
        if item.strip()
    )


def _load_manifest(args: argparse.Namespace, *, required: bool) -> StudyManifest | None:
    if args.study_manifest is None and args.study_manifest_sha256 is None:
        if required:
            raise SystemExit(
                "network modes require --study-manifest and --study-manifest-sha256"
            )
        return None
    if args.study_manifest is None or args.study_manifest_sha256 is None:
        raise SystemExit(
            "--study-manifest and --study-manifest-sha256 must be supplied together"
        )
    return load_study_manifest(
        Path(args.study_manifest),
        expected_sha256=args.study_manifest_sha256,
        expected_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        expected_symbols=tuple(sorted(SIGNAL_SYMBOLS)),
    )


def _dry_run_payload(
    args: argparse.Namespace, manifest: StudyManifest | None
) -> dict[str, object]:
    return {
        "mode": "dry_run",
        "network": False,
        "database_write": False,
        "broker_mutation": False,
        "collector_version": COLLECTOR_VERSION,
        "code_hash": runtime_code_hash(),
        "symbols": list(SYMBOLS),
        "signal_symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"],
        "predictor_only_symbols": ["BTCUSDT"],
        "rest_method": "GET",
        "rest_paths": sorted(REST_PATH_ALLOWLIST),
        "pit_columns": list(PIT_COLUMNS),
        "artifact_root_if_armed": str(Path(args.artifact_root).expanduser()),
        "collector_instance_id": args.collector_id,
        "replica_artifacts": [
            str(Path(path).expanduser()) for path in args.replica_artifact
        ],
        "minimum_healthy_replicas": args.minimum_healthy_replicas,
        "alert_webhook_count": len(_alert_webhook_urls()),
        "study_manifest_required_for_network": True,
        "study_manifest": (
            None
            if manifest is None
            else {
                "content_sha256": manifest.content_sha256,
                "effective_at": manifest.effective_at.isoformat(),
                "policy_hash": manifest.contract_hash,
                "study_id": manifest.study_id,
                "t0": manifest.t0.isoformat(),
            }
        ),
        "arm": f"{ENABLED_ENV}=true plus --run",
    }


def _offline_audit(root: Path) -> int:
    with AppendOnlyPITStore(root) as store:
        result = store.audit()
        result["samples"] = [redact_sample(row) for row in store.sample_by_source()]
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1


async def _run_collector(args: argparse.Namespace, manifest: StudyManifest) -> int:
    root = Path(args.artifact_root)
    epoch_observation_start = utc_now() if args.probe else None
    config = CollectorConfig(
        artifact_root=root,
        epoch_policy=manifest.epoch_policy,
        study_manifest=manifest,
        duration_seconds=args.duration,
        status_seconds=args.status_seconds,
        collector_instance_id=args.collector_id,
        replica_artifacts=tuple(Path(path) for path in args.replica_artifact),
        alert_webhook_urls=() if args.probe else _alert_webhook_urls(),
        minimum_healthy_replicas=(1 if args.probe else args.minimum_healthy_replicas),
        epoch_observation_start=epoch_observation_start,
    )
    with AppendOnlyPITStore(root, study_manifest=manifest) as store:
        collector = BinanceR4P0Collector(config, store)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, collector.stop.set)
        await collector.run()
        audit = store.audit()
        logging.info("collector.audit %s", json.dumps(audit, sort_keys=True))
        health = collector.health()
        logging.info("collector.health %s", json.dumps(health, sort_keys=True))
        return 0 if audit["ok"] and health["ok"] else 1


def main() -> int:
    args = parse_args()
    formatter = logging.Formatter(fmt="%(asctime)sZ %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.audit:
        if args.run or args.probe:
            raise SystemExit("--audit cannot be combined with network modes")
        return _offline_audit(Path(args.artifact_root))
    manifest = _load_manifest(
        args,
        required=bool(args.run or args.probe),
    )
    if not args.run and not args.probe:
        print(
            json.dumps(
                _dry_run_payload(args, manifest),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    assert manifest is not None
    if args.probe:
        if args.duration is None or not 1 <= args.duration <= 180:
            raise SystemExit("--probe requires --duration between 1 and 180 seconds")
    elif not _enabled():
        raise SystemExit(f"refusing --run: set {ENABLED_ENV}=true explicitly")
    if args.run:
        if args.collector_id == "r4-p0-local":
            raise SystemExit(
                "--run requires an explicit host-unique --collector-id "
                f"or {COLLECTOR_ID_ENV}"
            )
        configured_replicas = 1 + len(
            {str(Path(path).expanduser().resolve()) for path in args.replica_artifact}
        )
        if (
            args.minimum_healthy_replicas < 2
            or configured_replicas < args.minimum_healthy_replicas
        ):
            raise SystemExit(
                "--run requires at least two independently configured "
                "artifacts and --minimum-healthy-replicas >= 2"
            )
        if not _alert_webhook_urls() and not args.allow_log_only_alerts:
            raise SystemExit(
                f"--run requires {ALERT_WEBHOOKS_ENV}; "
                "use --allow-log-only-alerts only for explicit validation"
            )
    if args.duration is not None and args.duration <= 0:
        raise SystemExit("--duration must be positive")
    return asyncio.run(_run_collector(args, manifest))


if __name__ == "__main__":
    raise SystemExit(main())
