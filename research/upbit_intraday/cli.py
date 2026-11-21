"""Manual commands for upbit-intraday-v1. No scheduler, no orders, no DB.

python -m research.upbit_intraday.cli fetch --root DIR [--market KRW-BTC ...]
python -m research.upbit_intraday.cli develop --root DIR --selection-out FILE
python -m research.upbit_intraday.cli test --root DIR --selection FILE
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import fetch as fetch_mod
from .public_http import make_urllib_fetch, with_retries

# Frozen data window (matches rules.DATA_START_UTC / DATA_END_EXCLUSIVE_UTC; the
# loader refuses a manifest whose window differs).
DATA_START = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)
DATA_END_EXCLUSIVE = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


def _cmd_fetch(args: argparse.Namespace) -> int:
    fetch = with_retries(make_urllib_fetch(), attempts=6, base_delay=2.0)
    manifest = fetch_mod.collect(
        fetch,
        Path(args.root),
        DATA_START,
        DATA_END_EXCLUSIVE,
        markets=args.market or None,
        workers=args.workers,
        pacer=fetch_mod.Pacer(args.interval),
        progress=lambda m: print(m, file=sys.stderr, flush=True),
    )
    print(
        f"markets={len(manifest['markets'])} failures={len(manifest['failures'])}",
        file=sys.stderr,
    )
    return 1 if manifest["failures"] else 0


def _cmd_develop(args: argparse.Namespace) -> int:
    from . import run

    out = run.develop(Path(args.root))
    text = (
        json.dumps(out["selection"], sort_keys=True, indent=1, ensure_ascii=False)
        + "\n"
    )
    Path(args.selection_out).write_text(text)
    print(text)
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    from . import run

    results = run.test_look(
        Path(args.root),
        Path(args.selection),
        allow_additional_look=args.allow_additional_look,
    )
    print(json.dumps(results["verdicts"], sort_keys=True, indent=1))
    return 0


def _cmd_case_day(args: argparse.Namespace) -> int:
    from . import run

    root = Path(args.root)
    fetch = with_retries(make_urllib_fetch(), attempts=6, base_delay=2.0)
    case_root = root / "case_day"
    fetch_mod.collect(
        fetch,
        case_root,
        run.CASE_START,
        run.CASE_END_EXCLUSIVE,
        workers=args.workers,
        pacer=fetch_mod.Pacer(args.interval),
        dataset="upbit-intraday-v1-case-day",
    )
    out = run.case_day(case_root, Path(args.selection))
    (root / "case_day_results.json").write_text(
        json.dumps(out, sort_keys=True, indent=1) + "\n"
    )
    print(json.dumps(out["configs"], sort_keys=True, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="download 15-minute candles (public GET)")
    p.add_argument("--root", required=True, help="artifact directory outside the repo")
    p.add_argument("--market", action="append", help="limit to these markets (smoke)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--interval", type=float, default=0.12)
    p.set_defaults(func=_cmd_fetch)
    d = sub.add_parser("develop", help="develop-half grid and the frozen selection")
    d.add_argument("--root", required=True)
    d.add_argument("--selection-out", required=True)
    d.set_defaults(func=_cmd_develop)
    t = sub.add_parser(
        "test", help="the single test-half look (counted in the registry)"
    )
    t.add_argument("--root", required=True)
    t.add_argument("--selection", required=True)
    t.add_argument("--allow-additional-look", action="store_true")
    t.set_defaults(func=_cmd_test)
    c = sub.add_parser(
        "case-day", help="frozen rules over the retrospective day (illustration)"
    )
    c.add_argument("--root", required=True)
    c.add_argument("--selection", required=True)
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--interval", type=float, default=0.12)
    c.set_defaults(func=_cmd_case_day)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
