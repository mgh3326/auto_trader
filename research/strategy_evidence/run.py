"""CLI: ``fetch-dividends`` writes a public dividends snapshot, ``report`` the reports."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from research.strategy_evidence.dividends import fetch_dividends
from research.strategy_evidence.evidence import Evidence, compute
from research.strategy_evidence.report import Renderer
from research.trade_retro.export import ExportIntegrityError

FULL_REPORT_NAME = "report-full.md"


def _inside_repo(path: Path) -> bool:
    repo = Path(__file__).resolve().parents[2]
    return path.resolve().is_relative_to(repo)


def write_private_csvs(evidence: Evidence, private_dir: Path) -> None:
    """Row-level tables behind every section, for tracing a number to its rows."""
    evidence.tier.to_csv(private_dir / "q1_tier_orders.csv", index=False)
    evidence.buys.to_csv(private_dir / "q1_buys_by_lot_state.csv", index=False)
    evidence.add_rule.to_csv(private_dir / "q1_systematic_adds.csv", index=False)
    evidence.losscut_rows.to_csv(private_dir / "q2_losscut_touches.csv", index=False)
    evidence.intents.to_csv(private_dir / "q3_buy_intents.csv", index=False)
    evidence.parking_lots.to_csv(private_dir / "q4_parking_lots.csv", index=False)
    evidence.ladder_rows.to_csv(private_dir / "q5_crypto_sells.csv", index=False)
    rows = [
        {"market": m, "variant": v, "symbol": t.symbol, "entry_i": t.units[0].i,
         "units": len(t.units), "exit_i": t.exit_i, "exit_price": t.exit_price,
         "reason": t.reason, "gross": t.gross}
        for (m, v), trades in evidence.trades.items()
        for t in trades
    ]  # fmt: skip
    pd.DataFrame(rows).to_csv(private_dir / "rulesets_trades.csv", index=False)


def cmd_fetch(args: argparse.Namespace) -> int:
    manifest = fetch_dividends(Path(args.out), date(2026, 3, 22), date(2026, 11, 1))
    print({k: v["rows"] for k, v in manifest["entries"].items()})
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    private_dir = Path(args.private_dir).expanduser()
    if _inside_repo(private_dir):
        print("--private-dir must be outside the repository", file=sys.stderr)
        return 2
    evidence = compute(Path(args.export), Path(args.prices), Path(args.dividends))
    public_text = Renderer(evidence, public=True).render()
    private_text = Renderer(evidence, public=False).render()
    public_path = Path(args.public_out)
    private_path = private_dir / FULL_REPORT_NAME
    if args.check:
        stale = [
            str(path)
            for path, text in ((public_path, public_text), (private_path, private_text))
            if not path.is_file() or path.read_text(encoding="utf-8") != text
        ]
        if stale:
            print(f"differs from the regenerated report: {stale}", file=sys.stderr)
            return 1
        print("check passed: both reports match the regenerated text byte for byte")
        return 0
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(public_text, encoding="utf-8")
    private_dir.mkdir(parents=True, exist_ok=True)
    private_path.write_text(private_text, encoding="utf-8")
    write_private_csvs(evidence, private_dir)
    print(f"wrote {public_path} and {private_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-dividends", help="download distributions (network)")
    fetch.add_argument("--out", required=True)
    fetch.set_defaults(func=cmd_fetch)
    report = sub.add_parser("report", help="write the reports (no network)")
    report.add_argument("--export", required=True)
    report.add_argument("--prices", required=True)
    report.add_argument("--dividends", required=True)
    report.add_argument("--public-out", required=True)
    report.add_argument("--private-dir", required=True)
    report.add_argument("--check", action="store_true")
    report.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ExportIntegrityError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
