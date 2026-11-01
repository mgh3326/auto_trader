"""Data-plane commands (manual or Dagu-run; no scheduler code lives here).

    uv run python -m research.data_plane.cli collect --root DIR --market kr
    uv run python -m research.data_plane.cli health  --root DIR --market kr --dataset bars_1d

``collect`` exit codes: 0 every requested partition is written or was already
present; 2 a dataset could not be collected (source failure, coverage below
the floor, missing universe) — nothing was written for it; 3 the contract
refused a row (for example an observation dated after its partition day).
``health`` exits 0 when healthy, 1 otherwise, and 2 for a dataset that does not
apply to the market.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .bar_sources import (
    BAR_SOURCES,
    BROKER_SOURCES,
    DEFAULT_ADJUSTED_WINDOW,
    DEFAULT_SOURCE_ORDER,
)
from .budget import (
    BudgetError,
    BudgetSpec,
    ResearchBudget,
    parse_budgets,
)
from .calendars import local_today
from .collect import (
    DEFAULT_MAX_UNIVERSE_DROP,
    DEFAULT_MIN_COVERAGE,
    CollectError,
    collect,
)
from .contract import (
    DATASET_BARS_ADJUSTED,
    DATASETS,
    MARKETS,
    RefusedRowError,
    check_coverage_floor,
)
from .health import (
    DEFAULT_GRACE,
    DEFAULT_LOOKBACK_DAYS,
    check_grace,
    check_health,
    ping_url,
)
from .public_http import make_public_fetch


def _iso_now(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise argparse.ArgumentTypeError("--now needs a UTC offset")
    return ts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.data_plane.cli", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser(
        "collect", help="write today's (or --date's) partitions; idempotent"
    )
    c.add_argument(
        "--root", type=Path, required=True, help="artifact root (outside git)"
    )
    c.add_argument("--market", choices=sorted(MARKETS), required=True)
    c.add_argument(
        "--date",
        type=date.fromisoformat,
        help="partition day (default: today, market-local)",
    )
    c.add_argument(
        "--datasets",
        type=_dataset_list,
        default=None,
        help=(
            f"comma list from {','.join(DATASETS)} (default: every dataset of the "
            f"market, in that order; crypto has no {DATASET_BARS_ADJUSTED})"
        ),
    )
    c.add_argument(
        "--symbols",
        help="comma list: restrict bars/actions to these universe instruments",
    )
    c.add_argument("--min-coverage", type=_coverage_floor, default=DEFAULT_MIN_COVERAGE)
    c.add_argument(
        "--min-interval", type=float, default=0.3, help="seconds between requests"
    )
    c.add_argument(
        "--bar-sources",
        type=_source_list,
        help=(
            "KR/US bar source order with fallback, from "
            f"{','.join(BAR_SOURCES)} (default kis,toss,yahoo). Broker sources read "
            "the app's KIS/Toss credentials on this host"
        ),
    )
    c.add_argument(
        "--broker-budget",
        type=_budgets,
        default=parse_budgets(""),
        help=(
            "research share of each broker's call limit, BROKER=CALLS/SECONDS "
            "(default kis=2/1,toss=1/1; at most 25%% of the broker limit), shared "
            "by every collector using the same --root"
        ),
    )
    c.add_argument(
        "--adjusted-window",
        type=_positive_int,
        default=DEFAULT_ADJUSTED_WINDOW,
        help="sessions of adjusted bars kept in each bars_1d_adjusted vintage",
    )
    c.add_argument(
        "--max-universe-drop",
        type=float,
        default=DEFAULT_MAX_UNIVERSE_DROP,
        help="refuse a universe that lost more than this share of a venue (default 0.10)",
    )

    h = sub.add_parser(
        "health", help="exit non-zero on a gap, a stale dataset or a corrupt partition"
    )
    h.add_argument("--root", type=Path, required=True)
    h.add_argument("--market", choices=sorted(MARKETS), required=True)
    h.add_argument("--dataset", choices=DATASETS, required=True)
    h.add_argument("--lookback-days", type=_positive_int, default=DEFAULT_LOOKBACK_DAYS)
    h.add_argument(
        "--grace-hours",
        type=_grace_hours,
        default=DEFAULT_GRACE.total_seconds() / 3600,
        help="finite, non-negative hours after a bar's availability before it is due",
    )
    h.add_argument("--min-coverage", type=_coverage_floor, default=DEFAULT_MIN_COVERAGE)
    h.add_argument(
        "--ping-url-env",
        help="name of an environment variable holding a https ping URL (never printed)",
    )
    h.add_argument("--now", type=_iso_now, help=argparse.SUPPRESS)
    return parser


def _print_results(results) -> None:  # noqa: ANN001
    for r in results:
        print(
            f"{r.action} {r.dataset} {r.market} {r.partition_date} "
            f"rows={r.rows} sha256={r.sha256[:12]}"
        )


def _coverage_floor(value: str) -> float:
    try:
        return check_coverage_floor(float(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _grace_hours(value: str) -> float:
    try:
        hours = float(value)
        check_grace(timedelta(hours=hours))
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            f"--grace-hours must be a finite, non-negative number, got {value!r}"
        ) from exc
    return hours


def _dataset_list(value: str) -> list[str]:
    """A non-empty comma list of known datasets (an empty list would be a
    silent no-op that exits 0 having collected nothing)."""
    names = [d.strip() for d in value.split(",") if d.strip()]
    if not names:
        raise argparse.ArgumentTypeError("--datasets is empty")
    unknown = [d for d in names if d not in DATASETS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown datasets {unknown}; expected from {','.join(DATASETS)}"
        )
    return names


def _source_list(value: str) -> list[str]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names:
        raise argparse.ArgumentTypeError("--bar-sources is empty")
    unknown = [n for n in names if n not in BAR_SOURCES]
    if unknown or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(
            f"--bar-sources must be distinct names from {','.join(BAR_SOURCES)}"
        )
    return names


def _budgets(value: str) -> dict[str, BudgetSpec]:
    try:
        return parse_budgets(value)
    except BudgetError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        now = datetime.now(UTC)
        day = args.date or local_today(args.market, now)
        datasets = args.datasets
        symbols = (
            [x for x in (s.strip() for s in args.symbols.split(",")) if x]
            if args.symbols is not None
            else None
        )  # an explicit but empty --symbols is an error, never "no filter"
        brokers = None
        try:
            broker_kwargs: dict = {}
            if args.market in DEFAULT_SOURCE_ORDER:
                order = args.bar_sources or list(DEFAULT_SOURCE_ORDER[args.market])
                wanted = [n for n in order if n in BROKER_SOURCES]
                bars_requested = datasets is None or any(
                    d.startswith("bars_") for d in datasets
                )
                if wanted and bars_requested:
                    # lazy: the app clients load only on a broker-backed run
                    from .broker_adapters import open_broker_clients

                    brokers = open_broker_clients(wanted)
                broker_kwargs = {
                    "source_order": order,
                    "broker_clients": brokers.clients if brokers else {},
                    "source_unavailable": brokers.unavailable if brokers else {},
                    "budgets": {
                        name: ResearchBudget(
                            args.broker_budget[name],
                            state_path=args.root / ".budget" / f"{name}.json",
                        )
                        for name in wanted
                    },
                    "adjusted_window": args.adjusted_window,
                }
            elif args.bar_sources is not None:
                raise ValueError("--bar-sources applies to kr and us only")
            results = collect(
                args.root,
                market=args.market,
                day=day,
                datasets=datasets,
                fetch=make_public_fetch(min_interval=args.min_interval),
                symbols=symbols,
                min_coverage=args.min_coverage,
                max_universe_drop=args.max_universe_drop,
                **broker_kwargs,
            )
        except (RefusedRowError, CollectError) as exc:
            # Every dataset was attempted; print what succeeded, then the errors.
            _print_results(getattr(exc, "results", []))
            kind = "refused" if isinstance(exc, RefusedRowError) else "not collected"
            print(f"{kind}: {exc}", file=sys.stderr)
            for note in getattr(exc, "__notes__", []):
                print(note, file=sys.stderr)
            return 3 if isinstance(exc, RefusedRowError) else 2
        except ValueError as exc:
            print(f"not collected: {exc}", file=sys.stderr)
            return 2
        finally:
            if brokers is not None:
                brokers.close()
        _print_results(results)
        return 0

    now = args.now or datetime.now(UTC)
    try:
        report = check_health(
            args.root,
            market=args.market,
            dataset=args.dataset,
            now=now,
            lookback_days=args.lookback_days,
            grace=timedelta(hours=args.grace_hours),
            min_coverage=args.min_coverage,
        )
    except ValueError as exc:  # e.g. bars_1d_adjusted for crypto
        print(f"health not checked: {exc}", file=sys.stderr)
        return 2
    out = report.as_dict()
    out["ping"] = ping_url(args.ping_url_env, report.healthy)
    print(json.dumps(out, sort_keys=True))
    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
