"""Regenerate every ROB-1404 Part 2A table (+ v1 reconciliation, 2B feasibility).

    uv run python -m research.rob1404_kr_entry.run \
        --inputs <b1383 inputs dir> --v1-results <rob1383 results dir> --offline

Row-level CSVs go to ``research/rob1404_kr_entry/results/`` (gitignored --
order ids, budgets). ``--offline`` reads the price cache only; a cache miss
is a recorded missing row, never a silent drop.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

import pandas as pd

from research.rob1383_buy_drought.prices import KST, PriceStore
from research.rob1404_kr_entry import (
    explore_2b,
    inference,
    reconcile,
    sectors,
    versions,
)
from research.rob1404_kr_entry.clock import SessionCalendar
from research.rob1404_kr_entry.methods import METHODS, build_methods
from research.rob1404_kr_entry.outcomes import (
    BASE_COST,
    COST_GRID,
    GROSS,
    HORIZONS,
    Bench,
    evaluate,
    markouts,
)
from research.rob1404_kr_entry.population import Candidate, build

PKG = Path(__file__).resolve().parent
RESULTS = PKG / "results"
DEFAULT_CACHE = PKG / ".cache" / "prices"
INDEX_OF_SUFFIX = {".KS": "^KS11", ".KQ": "^KQ11"}
BASE = f"y__{BASE_COST.name}"
MEASURES = {
    "net_base": BASE,
    "gross": f"y__{GROSS.name}",
    "mkt_adj_net": f"y_mktadj__{BASE_COST.name}",
    "sector_adj_net": f"y_sectoradj__{BASE_COST.name}",
}
PAIRS = (
    ("nep", "cash"),
    ("next_day_open", "cash"),
    ("split_int", "cash"),
    ("resubmit3", "cash"),
    ("nep", "next_day_open"),
    ("split_int", "anchor_actual"),
)


def _write(frame: pd.DataFrame, name: str, out: Path) -> None:
    frame.to_csv(out / name, index=False, float_format="%.6g")
    print(f"wrote {out / name} ({len(frame)} rows)")


def _benches(
    store: PriceStore, table: dict[str, dict], universe: list[str]
) -> tuple[dict[str, Bench | None], dict[str, list[Bench]], dict[str, str | None]]:
    index: dict[str, Bench | None] = {}
    for idx in ("^KS11", "^KQ11"):
        b = store.bars("kr", idx)
        index[idx] = Bench(b, store.hourly("kr", idx)) if b is not None else None
    by_sector: dict[str, list[str]] = {}
    for s in universe:
        sec = (table.get(s) or {}).get("sector")
        if sec:
            by_sector.setdefault(sec, []).append(s)
    peer_bench: dict[str, list[Bench]] = {}
    sector_of = {s: (table.get(s) or {}).get("sector") for s in universe}
    cache: dict[str, Bench | None] = {}

    def bench(sym: str) -> Bench | None:
        if sym not in cache:
            b = store.bars("kr", sym)
            cache[sym] = Bench(b, store.hourly("kr", sym)) if b is not None else None
        return cache[sym]

    for s in universe:
        sec = sector_of.get(s)
        peers = [p for p in by_sector.get(sec, []) if p != s] if sec else []
        peer_bench[s] = [b for p in peers if (b := bench(p)) is not None]
    return index, peer_bench, sector_of


def _market_index(store: PriceStore, symbol: str) -> str | None:
    ticker = store.resolved_ticker.get(("kr", symbol)) or ""
    return next((v for k, v in INDEX_OF_SUFFIX.items() if ticker.endswith(k)), None)


def evaluate_all(
    cands: list[Candidate],
    orders_by_id: dict,
    store: PriceStore,
    table: dict,
    universe: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    index, peers, sector_of = _benches(store, table, universe)
    cal = master_calendar(store)
    rows, marks, legs = [], [], []
    for c in cands:
        meta = {
            "order_id": c.order_id,
            "symbol": c.symbol,
            "signal_at_kst": c.signal_at.astimezone(KST).isoformat(timespec="minutes"),
            "parent_id": c.parent_id,
            "parent_rank": c.parent_rank,
            "nxt_only": c.nxt_only,
            "strategy": c.strategy,
            "policy_version": c.policy_version,
            "policy_epoch": c.policy_epoch,
            "quantity": c.quantity,
            "anchor": c.anchor,
            "sector": sector_of.get(c.symbol),
        }
        bars = store.bars("kr", c.symbol)
        if bars is None:
            for m in METHODS:
                for h in HORIZONS:
                    rows.append(
                        {
                            **meta,
                            "method": m,
                            "horizon": h,
                            "status": "data_missing",
                            "missing_reason": store.missing.get(
                                ("kr", c.symbol), "no_bars"
                            ),
                        }
                    )
            continue
        hourly = store.hourly("kr", c.symbol)
        idx_name = _market_index(store, c.symbol)
        mkt = index.get(idx_name) if idx_name else None
        info = table.get(c.symbol) or {}
        is_etf = info.get("quote_type") == "ETF"
        methods = build_methods(c, orders_by_id[c.order_id], bars, hourly, cal)
        for name, m in methods.items():
            for i, leg in enumerate(m.legs):
                legs.append(
                    {
                        **meta,
                        "method": name,
                        "leg": i,
                        "price": leg.price,
                        "shares": leg.shares,
                        "state": leg.state,
                        "reason": leg.reason,
                        "fill_at": None if leg.at is None else str(leg.at),
                        "marketable": leg.marketable,
                        **{f"detail_{k}": v for k, v in m.detail.items()},
                    }
                )
            for h in HORIZONS:
                r = evaluate(
                    m,
                    bars,
                    hourly,
                    c.signal_at,
                    c.quantity,
                    h,
                    cal=cal,
                    is_etf=is_etf,
                    market_bench=mkt,
                    sector_peers=peers.get(c.symbol),
                    costs=(BASE_COST, GROSS, *COST_GRID),
                )
                rows.append({**meta, "market_index": idx_name, "is_etf": is_etf, **r})
            for mk in markouts(m, bars, mkt, cal):
                marks.append({**meta, **mk})
    return pd.DataFrame(rows), pd.DataFrame(marks), pd.DataFrame(legs)


def master_calendar(store: PriceStore) -> SessionCalendar:
    """Common KRX session list: the KOSPI index's daily bars (every candidate
    counts D0 and D0+h on it, not on its own rows)."""

    ks = store.bars("kr", "^KS11")
    if ks is None:
        raise SystemExit("^KS11 daily bars are required for the session calendar")
    return SessionCalendar(tuple(ks["session_date"]))


def _calendar(store: PriceStore) -> list:
    """Full session list for the block bootstrap; ``inference.summarise``
    slices it to each subset's first..last day."""

    return list(master_calendar(store).dates)


def _scopes(frame: pd.DataFrame):
    for level in ("parent", "order"):
        lv = frame if level == "order" else frame[frame["parent_rank"] == 0]
        for scope in ("all", "excl_nxt_only"):
            sc = lv if scope == "all" else lv[~lv["nxt_only"].astype(bool)]
            yield level, scope, sc


def summarise(detail: pd.DataFrame, calendar: list) -> pd.DataFrame:
    out = []
    for level, scope, sc in _scopes(detail):
        for h in HORIZONS:
            for m in METHODS:
                g = sc[(sc["horizon"] == h) & (sc["method"] == m)]
                status = g["status"].value_counts().to_dict()
                ok = g[g["status"] == "ok"]
                matured = g[g["status"].isin(["ok", "unknown"])]
                for label, col in MEASURES.items():
                    vals = ok[col].dropna() if col in ok else pd.Series(dtype=float)
                    sub = ok.loc[vals.index]
                    row: dict[str, Any] = {
                        "level": level,
                        "scope": scope,
                        "horizon": h,
                        "method": m,
                        "measure": label,
                        "n_candidates": len(g),
                        "n_ok": int(status.get("ok", 0)),
                        "n_unknown": int(status.get("unknown", 0)),
                        "n_immature": int(status.get("immature", 0)),
                        "n_data_missing": int(status.get("data_missing", 0)),
                        "n_measure_missing": int(len(ok) - len(vals)),
                        "n_any_fill_ok": int((ok["invested_frac"] > 0).sum())
                        if len(ok)
                        else 0,
                    }
                    if len(vals):
                        row.update(
                            inference.summarise(
                                (vals * 100).tolist(),
                                symbols=sub["symbol"].tolist(),
                                days=pd.to_datetime(sub["d0_session"]).dt.date.tolist(),
                                calendar=calendar,
                                parents=sub["parent_id"].tolist()
                                if level == "order"
                                else None,
                            )
                        )
                        row["verdict"] = inference.verdict(row["ci_lo"], row["ci_hi"])
                    if label == "net_base" and len(matured):
                        for b in ("if_unknown_unfilled", "if_unknown_filled"):
                            v = matured[f"{BASE}__{b}"].astype(float) * 100
                            row[f"bound_{b}_n"] = len(v)
                            row[f"bound_{b}_mean"] = float(v.mean())
                    out.append(row)
    return pd.DataFrame(out)


def paired(detail: pd.DataFrame, calendar: list) -> pd.DataFrame:
    out = []
    for level, scope, sc in _scopes(detail):
        for h in HORIZONS:
            g = sc[sc["horizon"] == h]
            for a, b in PAIRS:
                for label in ("net_base", "mkt_adj_net"):
                    col = MEASURES[label]
                    x = g[(g["method"] == a) & (g["status"] == "ok")].set_index(
                        "order_id"
                    )
                    y = g[(g["method"] == b) & (g["status"] == "ok")].set_index(
                        "order_id"
                    )
                    common = x.index.intersection(y.index)
                    d = (x.loc[common, col] - y.loc[common, col]).dropna()
                    row: dict[str, Any] = {
                        "level": level,
                        "scope": scope,
                        "horizon": h,
                        "pair": f"{a} - {b}",
                        "measure": label,
                        "n_pairs": len(d),
                    }
                    if len(d):
                        sub = x.loc[d.index]
                        row.update(
                            inference.summarise(
                                (d * 100).tolist(),
                                symbols=sub["symbol"].tolist(),
                                days=pd.to_datetime(sub["d0_session"]).dt.date.tolist(),
                                calendar=calendar,
                                parents=sub["parent_id"].tolist()
                                if level == "order"
                                else None,
                            )
                        )
                        row["verdict"] = inference.verdict(row["ci_lo"], row["ci_hi"])
                    out.append(row)
    return pd.DataFrame(out)


def cost_sensitivity(detail: pd.DataFrame, calendar: list) -> pd.DataFrame:
    out = []
    p = detail[(detail["parent_rank"] == 0) & (detail["status"] == "ok")]
    for h in HORIZONS:
        for m in ("nep", "next_day_open", "split_int", "resubmit3"):
            g = p[(p["horizon"] == h) & (p["method"] == m)]
            for cost in (GROSS, BASE_COST, *COST_GRID):
                v = g[f"y__{cost.name}"].dropna()
                if not len(v):
                    continue
                s = inference.summarise(
                    (v * 100).tolist(),
                    symbols=g.loc[v.index, "symbol"].tolist(),
                    days=pd.to_datetime(g.loc[v.index, "d0_session"]).dt.date.tolist(),
                    calendar=calendar,
                )
                out.append(
                    {
                        "level": "parent",
                        "scope": "all",
                        "horizon": h,
                        "method": m,
                        "cost": cost.name,
                        "commission": cost.commission,
                        "tax": cost.tax,
                        "slippage": cost.slippage,
                        "n": s["n"],
                        "mean": s["mean"],
                        "ci_lo": s.get("ci_lo"),
                        "ci_hi": s.get("ci_hi"),
                        "headline_ci": s.get("headline_ci"),
                        "verdict": inference.verdict(
                            s.get("ci_lo", math.nan), s.get("ci_hi", math.nan)
                        ),
                    }
                )
    return pd.DataFrame(out)


def block_sensitivity(detail: pd.DataFrame, calendar: list) -> pd.DataFrame:
    out = []
    p = detail[(detail["parent_rank"] == 0) & (detail["status"] == "ok")]
    for h in HORIZONS:
        for m in ("nep", "next_day_open", "split_int", "resubmit3"):
            g = p[(p["horizon"] == h) & (p["method"] == m)]
            v = g[BASE].dropna()
            for block in (5, 10, 20):
                if not len(v):
                    continue
                s = inference.summarise(
                    (v * 100).tolist(),
                    symbols=g.loc[v.index, "symbol"].tolist(),
                    days=pd.to_datetime(g.loc[v.index, "d0_session"]).dt.date.tolist(),
                    calendar=calendar,
                    block=block,
                )
                out.append(
                    {
                        "horizon": h,
                        "method": m,
                        "block": block,
                        "n": s["n"],
                        "mean": s["mean"],
                        "date_block_lo": s["date_block_lo"],
                        "date_block_hi": s["date_block_hi"],
                        "symbol_lo": s["symbol_lo"],
                        "symbol_hi": s["symbol_hi"],
                    }
                )
    return pd.DataFrame(out)


def markout_summary(marks: pd.DataFrame, calendar: list) -> pd.DataFrame:
    out = []
    if marks.empty:
        return pd.DataFrame()
    for level in ("parent", "order"):
        mk = marks if level == "order" else marks[marks["parent_rank"] == 0]
        for m in sorted(mk["method"].unique()):
            g = mk[mk["method"] == m]
            for k in (5, 20):
                for col in (f"markout{k}", f"markout{k}_mktadj"):
                    v = g[col].dropna().astype(float)
                    row = {
                        "level": level,
                        "method": m,
                        "measure": col,
                        "n_fills": len(g),
                        "n_matured": len(v),
                        "n_immature": int(
                            (g[f"markout{k}_status"] == "immature").sum()
                        ),
                        "n_data_missing": int(
                            g[f"markout{k}_status"]
                            .isin(["data_missing", "fill_not_on_calendar"])
                            .sum()
                        ),
                    }
                    if len(v):
                        s = inference.summarise(
                            (v * 100).tolist(),
                            symbols=g.loc[v.index, "symbol"].tolist(),
                            days=[
                                d.date()
                                for d in pd.to_datetime(g.loc[v.index, "fill_session"])
                            ],
                            calendar=calendar,
                        )
                        row.update(
                            {
                                k2: s[k2]
                                for k2 in (
                                    "mean",
                                    "median",
                                    "ci_lo",
                                    "ci_hi",
                                    "headline_ci",
                                )
                                if k2 in s
                            }
                        )
                    out.append(row)
    return pd.DataFrame(out)


def epoch_table(cands: list[Candidate], detail: pd.DataFrame) -> pd.DataFrame:
    rows = []
    c = pd.DataFrame(
        [
            {
                "epoch": x.policy_epoch,
                "version": x.policy_version,
                "parent": x.parent_rank == 0,
                "nxt_only": x.nxt_only,
                "order_id": x.order_id,
            }
            for x in cands
        ]
    )
    base = detail[(detail["method"] == "nep")]
    for epoch, g in c.groupby("epoch"):
        ids = set(g["order_id"])
        d = base[base["order_id"].isin(ids)]
        rows.append(
            {
                "policy_epoch": epoch,
                "versions": "|".join(sorted(set(g["version"].dropna()))),
                "n_orders": len(g),
                "n_parents": int(g["parent"].sum()),
                "n_nxt_only": int(g["nxt_only"].sum()),
                "n_parent_matured_d5": int(
                    (
                        (d["horizon"] == 5)
                        & d["status"].isin(["ok", "unknown"])
                        & (d["parent_rank"] == 0)
                    ).sum()
                ),
                "n_parent_matured_d20": int(
                    (
                        (d["horizon"] == 20)
                        & d["status"].isin(["ok", "unknown"])
                        & (d["parent_rank"] == 0)
                    ).sum()
                ),
            }
        )
    return pd.DataFrame(rows)


def parent_table(cands: list[Candidate]) -> pd.DataFrame:
    c = pd.DataFrame(
        [
            {"parent_id": x.parent_id, "symbol": x.symbol, "nxt_only": x.nxt_only}
            for x in cands
        ]
    )
    size = c.groupby("parent_id").size()
    return pd.DataFrame(
        [
            {"metric": "orders", "value": len(c)},
            {"metric": "parents", "value": int(size.size)},
            {"metric": "symbols", "value": int(c["symbol"].nunique())},
            {"metric": "parents_with_1_order", "value": int((size == 1).sum())},
            {"metric": "parents_with_2plus_orders", "value": int((size >= 2).sum())},
            {"metric": "max_orders_in_parent", "value": int(size.max())},
            {
                "metric": "orders_that_are_repeats(rank>0)",
                "value": int(len(c) - size.size),
            },
        ]
    )


def mde_table(pairs: pd.DataFrame, cands: list[Candidate]) -> pd.DataFrame:
    """Detectable effect now, and candidates/months needed, from *this* data's
    sd and design effect (parent level; no literature sample sizes)."""

    sig = [c.signal_at for c in cands if c.parent_rank == 0]
    months = (max(sig) - min(sig)).total_seconds() / (86400 * 30.4375)
    rate = len(sig) / months if months > 0 else math.nan
    out = []
    p = pairs[(pairs["level"] == "parent") & (pairs["scope"] == "all")]
    for _, r in p.iterrows():
        if not r.get("n_pairs"):
            continue
        row = {
            "horizon": r["horizon"],
            "pair": r["pair"],
            "measure": r["measure"],
            "n_pairs": r["n_pairs"],
            "sd_pp": r.get("sd"),
            "headline_ci": r.get("headline_ci"),
            "design_effect": r.get("design_effect"),
            "mde_now_pp": r.get("mde_one_sided_80"),
            "parents_per_month_observed": rate,
        }
        for delta in (0.5, 1.0, 2.0):
            n = inference.required_n(
                float(r.get("sd") or math.nan),
                delta,
                float(r.get("design_effect") or math.nan),
            )
            row[f"n_needed_delta_{delta:g}pp"] = n
            row[f"months_needed_delta_{delta:g}pp"] = (
                n / rate if rate and math.isfinite(n) else math.nan
            )
        out.append(row)
    return pd.DataFrame(out)


def explore_summary(ex: pd.DataFrame) -> pd.DataFrame:
    """2B feasibility counts (탐색): collisions with policy, trigger rate, n.

    Every column is read through ``col`` so a subset without a given field
    (e.g. no row with a valid ATR limit) yields counts of 0 and NaN stats,
    never a KeyError (tester r2 S3)."""

    def col(frame: pd.DataFrame, name: str) -> pd.Series:
        return (
            frame[name] if name in frame else pd.Series(dtype=object, index=frame.index)
        )

    def flag(frame: pd.DataFrame, name: str) -> pd.Series:
        return col(frame, name).fillna(False).astype(bool)

    def count(frame: pd.DataFrame, name: str) -> int:
        return int(flag(frame, name).sum())

    def quant(frame: pd.DataFrame, name: str, q: float) -> float:
        v = pd.to_numeric(col(frame, name), errors="coerce").dropna()
        return float(v.quantile(q)) if len(v) else math.nan

    rows = []
    for level in ("parent", "order"):
        g = ex if level == "order" else ex[col(ex, "parent_rank") == 0]
        a = g[flag(g, "atr_available")]  # ATR computable
        v = a[~flag(a, "atr_limit_nonpositive")]  # ... and a positive limit
        rows.append(
            {
                "label": "탐색(exploratory)",
                "level": level,
                "n_candidates": len(g),
                "n_atr_available": len(a),
                "n_atr_limit_nonpositive": count(a, "atr_limit_nonpositive"),
                "n_atr_limit_valid": len(v),
                "atr_pct_of_close_median": quant(v, "atr_pct_of_close", 0.5),
                "atr_limit_dist_below_close_pct_p10": quant(
                    v, "atr_limit_dist_below_close_pct", 0.1
                ),
                "atr_limit_dist_below_close_pct_median": quant(
                    v, "atr_limit_dist_below_close_pct", 0.5
                ),
                "atr_limit_dist_below_close_pct_p90": quant(
                    v, "atr_limit_dist_below_close_pct", 0.9
                ),
                "n_atr_limit_shallower_than_deep_min_3pct": count(
                    v, "atr_limit_shallower_than_deep_min"
                ),
                "n_atr_limit_deeper_than_deep_max_12pct": count(
                    v, "atr_limit_deeper_than_deep_max"
                ),
                "n_atr_limit_above_actual_anchor": int(
                    (
                        pd.to_numeric(
                            col(v, "atr_limit_vs_anchor_pct"), errors="coerce"
                        )
                        > 0
                    ).sum()
                ),
                "atr_limit_vs_anchor_pct_median": quant(
                    v, "atr_limit_vs_anchor_pct", 0.5
                ),
                "anchor_dist_below_close_pct_median": quant(
                    v, "anchor_dist_below_close_pct", 0.5
                ),
                "n_rebound_observable_hours_ge1": int(
                    (
                        pd.to_numeric(
                            col(v, "rebound_hours_observable"), errors="coerce"
                        )
                        >= 1
                    ).sum()
                ),
                "n_rebound_triggered": count(v, "rebound_triggered"),
                "rebound_cap_vs_close_t_pct_median": quant(
                    v, "rebound_cap_vs_close_t_pct", 0.5
                ),
                "n_matured_d5": count(g, "matured_d5"),
                "n_matured_d20": count(g, "matured_d20"),
            }
        )
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--v1-results", type=Path, required=True)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=RESULTS)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    args.out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    repo = PKG.parents[1]

    store = PriceStore(cache_dir=args.cache_dir, offline=args.offline)
    cands, ledger, orders = build(args.inputs, store)
    orders_by_id = {o.order_id: o for o in orders}
    universe = sectors.kr_symbols_in_cache(args.cache_dir)
    table = sectors.load_or_fetch(args.cache_dir, store, universe, offline=args.offline)

    detail, marks, legs = evaluate_all(cands, orders_by_id, store, table, universe)
    d0 = pd.to_datetime(detail["d0_session"].dropna()).dt.date
    calendar = _calendar(store)
    _write(pd.DataFrame(ledger.rows), "population_ledger.csv", args.out)
    _write(detail, "outcomes_detail.csv", args.out)
    _write(legs, "legs_detail.csv", args.out)
    _write(marks, "markouts_detail.csv", args.out)
    _write(summarise(detail, calendar), "summary.csv", args.out)
    pairs = paired(detail, calendar)
    _write(pairs, "paired.csv", args.out)
    _write(mde_table(pairs, cands), "mde.csv", args.out)
    _write(cost_sensitivity(detail, calendar), "cost_sensitivity.csv", args.out)
    _write(block_sensitivity(detail, calendar), "block_sensitivity.csv", args.out)
    _write(markout_summary(marks, calendar), "markouts_summary.csv", args.out)
    _write(epoch_table(cands, detail), "policy_epochs.csv", args.out)
    _write(parent_table(cands), "parents.csv", args.out)

    # ---- v1 reproduction + canonical figures
    fresh = reconcile.rerun_v1_detail(orders, store)
    diff = reconcile.diff_against_saved(fresh, args.v1_results / "q2_q3_detail.csv")
    _write(pd.DataFrame([diff]), "v1_reproduction.csv", args.out)
    _write(
        pd.DataFrame(reconcile.canonical_rows(fresh, args.v1_results / "q4_kr.csv")),
        "canonical_reconcile.csv",
        args.out,
    )

    # ---- 2B feasibility (exploratory counts only)
    ex = []
    for c in cands:
        bars = store.bars("kr", c.symbol)
        if bars is None:
            continue
        ex.append(
            {
                "order_id": c.order_id,
                "symbol": c.symbol,
                "parent_rank": c.parent_rank,
                "nxt_only": c.nxt_only,
                "d0": None,
                **explore_2b.candidate_row(
                    bars, store.hourly("kr", c.symbol), c.signal_at, c.anchor
                ),
            }
        )
    ex_df = pd.DataFrame(ex)
    _write(ex_df, "explore_2b_detail.csv", args.out)
    _write(explore_summary(ex_df), "explore_2b_summary.csv", args.out)

    # ---- versions
    _write(versions.input_table(args.inputs), "versions_inputs.csv", args.out)
    syms = sorted({c.symbol for c in cands}) + ["^KS11", "^KQ11"]
    _write(
        versions.cache_table(args.cache_dir, syms), "versions_price_cache.csv", args.out
    )
    sig = pd.Series([c.signal_at for c in cands])
    meta = {
        **versions.git_info(repo),
        **versions.cache_summary(args.cache_dir),
        "sectors_fetched_at": sectors.fetched_at(args.cache_dir),
        "sector_coverage_candidates": f"{sum(1 for s in {c.symbol for c in cands} if (table.get(s) or {}).get('sector'))}/{len({c.symbol for c in cands})}",
        "sector_universe": f"{sum(1 for s in universe if (table.get(s) or {}).get('sector'))}/{len(universe)}",
        "first_signal_kst": min(sig).astimezone(KST).isoformat(timespec="minutes"),
        "last_signal_kst": max(sig).astimezone(KST).isoformat(timespec="minutes"),
        "first_d0": str(min(d0)),
        "last_d0": str(max(d0)),
        "calendar_sessions": len(calendar),
        "price_cache_last_session": str(
            store.bars("kr", "^KS11")["session_date"].max()
        ),
        "offline": args.offline,
    }
    (args.out / "run_meta.json").write_text(
        json.dumps(meta, indent=1, ensure_ascii=False, default=str)
    )
    print(json.dumps(meta, indent=1, ensure_ascii=False, default=str))
    print(f"done in {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
