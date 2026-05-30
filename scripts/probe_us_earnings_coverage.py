#!/usr/bin/env python3
"""US earnings event -> price coverage probe (ROB-371).

Operator-run, **strictly read-only** probe that measures how well realized
Finnhub US earnings events join the ``us_candles_1d`` ``-5..+20d`` daily window,
then evaluates the ROB-367 §5 readiness gate and emits a counts-only PASS/FAIL
artifact. It NEVER writes to any database and NEVER mutates broker/order state.

Default is a dry-run; ``--run`` performs the (read-only) DB measurement. The
only network call is the opt-in ``--measure-delisted-recoverability`` Yahoo
probe (no DB write).

Materializing a dev-DB window (when the verdict is "coverage not materialized")
is a SEPARATE, explicitly operator-run step using the existing backfill CLI
against a dev database — never this probe:

    uv run python scripts/backfill_daily_candles.py \\
        --market us --symbols AAPL,MSFT,... --horizon-bars 60

Then re-run this probe. See docs/runbooks/rob-371-us-earnings-coverage-probe.md.

Exit codes: 0 = gate PASS, 1 = gate FAIL, 2 = error.

Examples:
    uv run python -m scripts.probe_us_earnings_coverage \\
        --from-date 2024-01-01 --to-date 2025-05-30 --run --out
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "us_earnings_coverage.v1"


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(
        description="Read-only US earnings event->price coverage probe (ROB-371)."
    )
    parser.add_argument(
        "--from-date",
        type=_parse_iso,
        default=today - timedelta(days=365),
        dest="from_date",
        help="ISO start date (default: 365 days ago).",
    )
    parser.add_argument(
        "--to-date",
        type=_parse_iso,
        default=today,
        dest="to_date",
        help="ISO end date (default: today).",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Perform the read-only DB measurement. Default is dry-run.",
    )
    parser.add_argument(
        "--out",
        action="store_true",
        help=(
            "Write the counts-only artifact under AUTO_TRADER_RESEARCH_ARTIFACT_ROOT"
            " (or the gitignored research/event_coverage/results/ fallback)."
        ),
    )
    parser.add_argument(
        "--measure-delisted-recoverability",
        action="store_true",
        dest="measure_delisted_recoverability",
        help="Opt-in: probe Yahoo for delisted-symbol bar recovery (read-only).",
    )
    parser.add_argument(
        "--delisted-sample",
        type=int,
        default=10,
        dest="delisted_sample",
        help="Max delisted symbols to probe for recoverability (default 10).",
    )
    args = parser.parse_args(argv)
    args.dry_run = not args.run
    return args


def build_artifact(
    measurement,
    gate_result,
    *,
    from_date: date,
    to_date: date,
    backfill_performed: bool,
) -> dict:
    """Counts-only artifact. ``measurement`` is a frozen dataclass of scalars, so
    ``asdict`` cannot emit symbol/bar-date collections (B3)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "market": "us",
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "backfill_performed": backfill_performed,
        "passed": gate_result.passed,
        "verdict": gate_result.verdict,
        "measurement": dataclasses.asdict(measurement),
        "criteria": [
            {
                "name": c.name,
                "observed": c.observed,
                "threshold": c.threshold,
                "passed": c.passed,
                "note": c.note,
            }
            for c in gate_result.criteria
        ],
    }


def _emit_dry_run(args: argparse.Namespace) -> int:
    """Dry-run summary. Uses ONLY stdlib logging and imports no app/settings
    modules, so the default no-``--run`` path exits 0 with no KIS/Upbit/OpenDART/
    DATABASE_URL/SECRET_KEY present (Blocker 1)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logger.info(
        "[DRY-RUN] would measure US earnings coverage %s..%s "
        "(delisted_recoverability=%s, out=%s). Read-only, no DB access, no "
        "mutation. Pass --run to execute.",
        args.from_date,
        args.to_date,
        args.measure_delisted_recoverability,
        args.out,
    )
    return 0


async def _run_measurement(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.market_events.coverage_gate import (
        Section5Thresholds,
        evaluate_section5_gate,
    )
    from app.services.market_events.us_earnings_coverage import (
        UsEarningsCoverageService,
    )

    async with AsyncSessionLocal() as db:
        measurement = await UsEarningsCoverageService(db).measure(
            from_date=args.from_date,
            to_date=args.to_date,
            measure_delisted_recoverability=args.measure_delisted_recoverability,
            delisted_sample=args.delisted_sample,
        )

    gate = evaluate_section5_gate(measurement, Section5Thresholds())
    artifact = build_artifact(
        measurement,
        gate,
        from_date=args.from_date,
        to_date=args.to_date,
        backfill_performed=False,
    )
    print(json.dumps(artifact))
    print(gate.verdict)

    if args.out:
        from research.event_coverage.artifact_paths import coverage_artifact_path

        path = coverage_artifact_path("us_earnings_coverage.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2))
        logger.info("artifact written: %s", path)

    return 0 if gate.passed else 1


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.run:
        # Dry-run: no app/settings import, no secrets required (Blocker 1).
        return _emit_dry_run(args)

    # --run path only: now it is safe to load settings-backed logging/Sentry.
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="probe-us-earnings-coverage")
    try:
        return await _run_measurement(args)
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        logger.exception("probe_us_earnings_coverage crashed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
