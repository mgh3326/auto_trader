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
        epoch_path = root / "epochs" / f"{args.market}.json"
        epoch = (
            json.loads(epoch_path.read_bytes())
            if epoch_path.exists()
            else {"start_asof": str(asof), "context": context}
        )
        if epoch["context"] != context:
            raise ValueError("frozen epoch context changed; use a new ledger epoch")
        epoch_start = date.fromisoformat(epoch["start_asof"])
        if epoch_start > asof:
            raise ValueError("asof predates the pinned epoch start")
        if not args.dry_run:
            store.publish(epoch_path, store.json_bytes(epoch))
        start = (
            date.fromisoformat(previous["asof"]) + timedelta(days=1)
            if previous
            else epoch_start
        )
        # Include the preceding weeks to resolve next-open dates without snapping old intents to D0.
        calendar_start = start - timedelta(days=14)
        sessions = inputs.calendar(
            args.market, calendar_start, asof + timedelta(days=370)
        )
        due = [day for day in sessions if start <= day <= asof]
        signals, input_state = inputs.signal_feed(
            Path(args.signals).expanduser() if args.signals else None,
            args.market,
            names,
            asof,
            due[0],
        )
        signals = [
            signal
            for signal in signals
            if (day := engine.entry_day(args.market, signal.at, sessions)) is not None
            and start <= day <= asof
        ]
        bars, failures = inputs.public_bars(args.market, names, start, asof)
        required: set[tuple[str, date]] = set()
        for signal in signals:
            entry = engine.entry_day(args.market, signal.at, sessions)
            if entry is not None:
                required.update((signal.symbol, day) for day in due if day >= entry)
        for episode in previous["episodes"] if previous else []:
            required.update(
                (episode["symbol"], day)
                for day in due
                if episode["entry_day"] <= str(day)
                and (
                    Decimal(episode["candidate"]["qty"]) > 0
                    or str(day) <= episode["h_day"]
                )
            )
        gaps = [
            f"{name}:{day}"
            for day in sessions
            if start <= day <= asof
            for name in names
            if (name, day) not in bars
        ]
        blocking_gaps = [
            f"{name}:{day}"
            for name, day in sorted(required, key=lambda pair: (pair[1], pair[0]))
            if (name, day) not in bars
        ]
        if blocking_gaps:
            input_state = (
                "unverified:missing_bars"
                if input_state == "verified"
                else input_state + ";missing_bars"
            )
        report = engine.advance(
            args.market,
            asof,
            previous,
            sessions,
            bars,
            signals,
            cost,
            input_state,
            start_day=start,
        )
        report.update(
            {
                "mode": "dry-run" if args.dry_run else "shadow",
                "committed": not args.dry_run and input_state == "verified",
                "start_asof": epoch["start_asof"],
                "required_signal_history_from": str(due[0]),
                "context": context,
                "previous_sha256": previous_hash,
                "data_gaps": gaps,
                "blocking_data_gaps": blocking_gaps,
                "fetch_failures": failures,
                "coverage_gaps": (previous.get("coverage_gaps", []) if previous else [])
                + ([str(day) for day in due] if input_state != "verified" else []),
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
        if report["committed"]:
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
                    "committed",
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
    return 3 if not args.dry_run and not report["committed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
