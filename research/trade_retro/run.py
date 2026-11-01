"""CLI: ``fetch-prices`` writes a public price snapshot, ``report`` writes the reports."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pandas as pd

from research.trade_retro.audit import (
    economic_duplicates,
    external_refs_on_other_rows,
    join_coverage,
    non_session_stamps,
    order_waterfall,
    quantity_mismatches,
)
from research.trade_retro.canonical import (
    apply_splits,
    build_canon,
    reclassify_implausible,
)
from research.trade_retro.entry import build_executions, classify_orders
from research.trade_retro.export import ExportIntegrityError, load_export
from research.trade_retro.idle import daily_account, summarize_idle
from research.trade_retro.positions import (
    cutover_time,
    match_frame,
    position_frame,
    reconstruct,
)
from research.trade_retro.prices import fetch_snapshot, load_snapshot, snapshot_range
from research.trade_retro.report import Renderer, Results
from research.trade_retro.rules import (
    hold_instead_of_sell,
    lot_entry_types,
    time_exit_counterfactual,
    unfilled_buy_counterfactual,
)
from research.trade_retro.sensitivity import run_scenarios

FULL_REPORT_NAME = "report-full.md"


def compute(export_dir: Path, prices_dir: Path) -> Results:
    export = load_export(export_dir)
    book = load_snapshot(prices_dir)
    canon = build_canon(export)
    fills, split_adjusted = apply_splits(canon.fills, book)
    fills, implausible = reclassify_implausible(fills, book)
    orders = classify_orders(canon.orders, book)
    walk = reconstruct(fills, book)
    matches = match_frame(walk, book)
    positions = position_frame(matches)
    executions = build_executions(fills, orders, book)
    unknown = pd.DataFrame(
        [asdict(u) for u in walk.unknown_sells],
        columns=["alias", "market", "symbol", "qty", "price", "day", "pre_cutover"],
    )
    cutover = cutover_time(fills)
    idle: dict = {}
    idle_meta: dict = {}
    for (alias, market), _ in fills.groupby(["alias", "market"], sort=True):
        frame, meta = daily_account(fills, book, alias, market, cutover)
        idle[(alias, market)] = summarize_idle(frame)
        idle_meta[(alias, market)] = meta
    return Results(
        manifest=export.manifest,
        manifest_sha256=export.manifest_sha256,
        book=book,
        diagnostics=canon.diagnostics,
        orders=orders,
        fills=fills,
        matches=matches,
        positions=positions,
        unknown_sells=unknown,
        executions=executions,
        idle=idle,
        idle_meta=idle_meta,
        r1=unfilled_buy_counterfactual(orders, book),
        r2=hold_instead_of_sell(executions),
        r3=time_exit_counterfactual(matches, book),
        r4=lot_entry_types(matches, executions),
        embedded_buys=walk.embedded_buys,
        split_adjusted=split_adjusted,
        implausible=implausible,
        unpriced_open=walk.unpriced_open,
        audit={
            "economic": economic_duplicates(export, canon),
            "quantity": quantity_mismatches(export, canon),
            "join": join_coverage(executions, orders),
            "refs_elsewhere": external_refs_on_other_rows(executions, orders),
            "stamps": non_session_stamps(export),
            "orders": order_waterfall(orders),
        },
        scenarios=run_scenarios(export, book),
        cutover=cutover,
    )


def _inside_repo(path: Path) -> bool:
    repo = Path(__file__).resolve().parents[2]
    return path.resolve().is_relative_to(repo)


def cmd_fetch(args: argparse.Namespace) -> int:
    export = load_export(Path(args.export))
    canon = build_canon(export)
    pairs = pd.concat(
        [canon.orders[["market", "symbol"]], canon.fills[["market", "symbol"]]]
    ).drop_duplicates()
    symbols = {m: sorted(g["symbol"]) for m, g in pairs.groupby("market")}
    window = export.manifest["window"]
    start, end = snapshot_range(
        date.fromisoformat(window["since_utc"][:10]),
        date.fromisoformat(window["until_utc"][:10]),
    )
    manifest = fetch_snapshot(Path(args.out), symbols, start, end)
    print(f"bars: {len(manifest['entries'])} missing: {sorted(manifest['missing'])}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    private_dir = Path(args.private_dir).expanduser()
    if _inside_repo(private_dir):
        print("--private-dir must be outside the repository", file=sys.stderr)
        return 2
    results = compute(Path(args.export), Path(args.prices))
    public_text = Renderer(results, public=True).render()
    private_text = Renderer(results, public=False).render()
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
    results.positions.to_csv(private_dir / "positions.csv", index=False)
    results.matches.to_csv(private_dir / "matches.csv", index=False)
    results.executions.to_csv(private_dir / "executions.csv", index=False)
    print(f"wrote {public_path} and {private_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-prices", help="download public daily bars (network)")
    fetch.add_argument("--export", required=True)
    fetch.add_argument("--out", required=True)
    fetch.set_defaults(func=cmd_fetch)
    report = sub.add_parser("report", help="write the reports (no network)")
    report.add_argument("--export", required=True)
    report.add_argument("--prices", required=True)
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
