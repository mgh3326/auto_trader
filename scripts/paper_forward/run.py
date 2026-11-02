"""Manual, no-order daily forward shadow. See docs/runbooks/paper-forward.md."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.paper_forward import engine, inputs, store  # noqa: E402
from research.strategy_evidence import decision_rules  # noqa: E402
from research.strategy_evidence.common import ROUND_TRIP_COST  # noqa: E402


def run(args: argparse.Namespace) -> dict:
    now = datetime.now(UTC)
    asof = (
        date.fromisoformat(args.asof)
        if args.asof
        else inputs.last_completed(args.market, now)
    )
    if (
        asof not in inputs.calendar(args.market, asof, asof)
        or inputs.close_time(args.market, asof) > now
    ):
        raise ValueError("asof must be a completed market session")
    root = Path(args.ledger_dir).expanduser().resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("ledger/report directory must be outside the repository")
    with store.locked(root, args.market):
        existing = root / "ledger" / args.market / f"{asof}.json"
        previous, previous_hash = store.latest(root, args.market)
        if existing.is_file():
            report = json.loads(existing.read_bytes())
            store.reports(root, report)  # repair a crash after ledger commit
            return report
        if previous and previous["asof"] >= str(asof):
            raise ValueError("cannot insert a past day into the ledger")
        names = inputs.universe(Path(args.universe).expanduser(), args.market)
        cost = Decimal(str(ROUND_TRIP_COST[args.market]))
        context = {
            "universe_sha256": inputs.digest(Path(args.universe).expanduser()),
            "rules_sha256": inputs.digest(Path(decision_rules.__file__)),
            "engine_sha256": inputs.digest(Path(engine.__file__)),
            "cost": str(cost),
        }
        if previous and previous["context"] != context:
            raise ValueError(
                "frozen universe/rules/cost changed; use a new ledger epoch"
            )
        signals, input_state = inputs.signal_feed(
            Path(args.signals).expanduser() if args.signals else None,
            args.market,
            names,
            asof,
        )
        start = (
            date.fromisoformat(previous["asof"]) + timedelta(days=1)
            if previous
            else asof
        )
        # Include the preceding weeks to resolve next-open dates without snapping old intents to D0.
        calendar_start = start - timedelta(days=14)
        sessions = inputs.calendar(
            args.market, calendar_start, asof + timedelta(days=370)
        )
        signals = [s for s in signals if s.at.astimezone(UTC).date() >= calendar_start]
        bars, failures = inputs.public_bars(args.market, names, start, asof)
        gaps = [
            f"{name}:{day}"
            for day in sessions
            if start <= day <= asof
            for name in names
            if (name, day) not in bars
        ]
        if gaps:
            input_state = (
                "unverified:missing_bars"
                if input_state == "verified"
                else input_state + ";missing_bars"
            )
        report = engine.advance(
            args.market, asof, previous, sessions, bars, signals, cost, input_state
        )
        report.update(
            {
                "mode": "dry-run" if args.dry_run else "shadow",
                "context": context,
                "previous_sha256": previous_hash,
                "data_gaps": gaps,
                "fetch_failures": failures,
                "coverage_gaps": (previous.get("coverage_gaps", []) if previous else [])
                + ([str(asof)] if input_state != "verified" else []),
                "bar_inputs": {
                    f"{s}:{d}": {
                        "open": str(b.open),
                        "close": str(b.close),
                        "split": str(b.split),
                    }
                    for (s, d), b in sorted(bars.items())
                },
                "signal_feed_sha256": inputs.digest(Path(args.signals).expanduser())
                if args.signals and Path(args.signals).expanduser().is_file()
                else None,
                "bars_sha256": hashlib.sha256(
                    store.json_bytes(
                        {
                            f"{s}:{d}": {
                                "open": str(b.open),
                                "close": str(b.close),
                                "split": str(b.split),
                            }
                            for (s, d), b in sorted(bars.items())
                        }
                    )
                ).hexdigest(),
            }
        )
        if not args.dry_run:
            store.publish(existing, store.json_bytes(report))
        store.reports(root, report)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("crypto", "us"), required=True)
    parser.add_argument("--asof", help="bar date; default last completed session")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="write preview reports without advancing the ledger",
    )
    parser.add_argument(
        "--ledger-dir",
        default=os.environ.get(
            "PAPER_FORWARD_DIR", "~/.local/share/auto_trader/paper-forward"
        ),
    )
    parser.add_argument(
        "--universe",
        default=os.environ.get("PAPER_FORWARD_UNIVERSE"),
        help="frozen research prices_manifest.json",
    )
    parser.add_argument(
        "--signals",
        default=os.environ.get("PAPER_FORWARD_SIGNALS"),
        help="normalized intent feed (never inferred from prices)",
    )
    args = parser.parse_args(argv)
    if not args.universe:
        parser.error("--universe or PAPER_FORWARD_UNIVERSE is required")
    try:
        report = run(args)
    except (ValueError, OSError, KeyError, ArithmeticError) as exc:
        print(f"refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "market",
                    "asof",
                    "mode",
                    "input_state",
                    "signals",
                    "summary",
                    "ledger_diff",
                    "episode_report",
                    "data_gaps",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
