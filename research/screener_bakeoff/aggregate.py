"""Aggregate picks.csv into the bakeoff scorecards.

    uv run python -m research.screener_bakeoff.aggregate

Reads only artifacts written by run_bakeoff.py.
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

from research.screener_bakeoff.scoring import summarise


def _bench_map(df: pd.DataFrame) -> dict:
    b = df[df["source_id"].str.endswith(".benchmark")]
    return {(r.market, r.decision_date, r.horizon): r.ret for r in b.itertuples()}


def build(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    bench = _bench_map(df)
    df = df.copy()
    df["bench_ret"] = [
        bench.get((m, d, h))
        for m, d, h in zip(
            df["market"], df["decision_date"], df["horizon"], strict=False
        )
    ]
    df["excess"] = df["ret"] - df["bench_ret"]

    rows = []
    picks = df[~df["source_id"].str.endswith(".benchmark")]
    for (market, sid, gate, horizon, window), grp in picks.groupby(
        ["market", "source_id", "gate", "horizon", "window"], dropna=False
    ):
        rows.append(_row(market, sid, gate, horizon, window, grp))
    for (market, sid, gate, horizon), grp in picks.groupby(
        ["market", "source_id", "gate", "horizon"], dropna=False
    ):
        rows.append(_row(market, sid, gate, horizon, "all", grp))
    # benchmark rows, so the report can print the market baseline alongside
    for (market, horizon), grp in df[
        df["source_id"].str.endswith(".benchmark")
    ].groupby(["market", "horizon"]):
        rows.append(_row(market, f"{market}.benchmark", "none", horizon, "all", grp))
    scorecard = pd.DataFrame(rows)

    return scorecard, df


def _row(market, sid, gate, horizon, window, grp) -> dict:
    ret = grp["ret"].to_numpy(dtype=float)
    exc = grp["excess"].to_numpy(dtype=float)
    strict = grp[grp["status"] == "full"]["ret"].to_numpy(dtype=float)
    uncensored = grp[~grp["censored"].astype(bool)]
    un_ret = uncensored["ret"].to_numpy(dtype=float)
    un_exc = uncensored["excess"].to_numpy(dtype=float)
    un = summarise(un_ret)
    unx = summarise(un_exc)
    delisted = int(
        ((grp["status"] == "truncated") & (~grp["censored"].astype(bool))).sum()
    )
    st = summarise(ret)
    ex = summarise(exc)
    sx = summarise(strict)
    return {
        "market": market,
        "source_id": sid,
        "gate": gate,
        "horizon": horizon,
        "window": window,
        "dates": int(grp["decision_date"].nunique()),
        "picks": int(len(grp)),
        "scored": st["n"],
        "missing": int((grp["status"] == "missing").sum()),
        "truncated": int((grp["status"] == "truncated").sum()),
        "censored": int(grp["censored"].astype(bool).sum()),
        "delisted_or_gap": delisted,
        "n_unc": un["n"],
        "mean_ret_unc": un["mean"],
        "median_ret_unc": un["median"],
        "win_rate_unc": un["win_rate"],
        "mean_excess_unc": unx["mean"],
        "excess_t_unc": unx["t_stat"],
        "excess_win_rate_unc": unx["win_rate"],
        "mean_ret": st["mean"],
        "median_ret": st["median"],
        "win_rate": st["win_rate"],
        "p10": st["p10"],
        "p90": st["p90"],
        "t_stat": st["t_stat"],
        "mean_excess": ex["mean"],
        "median_excess": ex["median"],
        "excess_win_rate": ex["win_rate"],
        "excess_t": ex["t_stat"],
        "mean_ret_strict": sx["mean"],
        "mean_mfe": float(np.nanmean(grp["mfe"])) if grp["mfe"].notna().any() else None,
        "mean_mae": float(np.nanmean(grp["mae"])) if grp["mae"].notna().any() else None,
        "mean_hl_mfe": float(np.nanmean(grp["hl_mfe"]))
        if grp["hl_mfe"].notna().any()
        else None,
        "mean_hl_mae": float(np.nanmean(grp["hl_mae"]))
        if grp["hl_mae"].notna().any()
        else None,
        "mean_pool": float(grp["pool_size"].mean()),
        "mean_gated": float(grp["gated_size"].mean()),
    }


def date_level(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse to one observation per decision date before summarising.

    Pick-level t-stats are meaningless here: the 10 picks of one date share a
    market day, and consecutive decision dates share up to h-1 forward
    sessions.  Date-level aggregation removes the first (cross-sectional)
    duplication; the second (overlapping windows) remains and is stated in the
    report — every t below is DESCRIPTIVE ONLY.
    """
    picks = df[~df["source_id"].str.endswith(".benchmark")]
    picks = picks[~picks["censored"].astype(bool)]
    daily = (
        picks.groupby(
            ["market", "source_id", "gate", "horizon", "window", "decision_date"]
        )
        .agg(ret=("ret", "mean"), excess=("excess", "mean"), n=("ret", "size"))
        .reset_index()
    )
    out = []
    for keys, grp in daily.groupby(
        ["market", "source_id", "gate", "horizon", "window"]
    ):
        for window_label, sub in ((keys[4], grp),):
            exc = sub["excess"].to_numpy(dtype=float)
            ret = sub["ret"].to_numpy(dtype=float)
            st, ex = summarise(ret), summarise(exc)
            out.append(
                {
                    "market": keys[0],
                    "source_id": keys[1],
                    "gate": keys[2],
                    "horizon": keys[3],
                    "window": window_label,
                    "dates": int(len(sub)),
                    "mean_ret": st["mean"],
                    "median_ret": st["median"],
                    "mean_excess": ex["mean"],
                    "median_excess": ex["median"],
                    "dates_beating_market": ex["win_rate"],
                    "excess_t_descriptive": ex["t_stat"],
                }
            )
    allw = (
        daily.groupby(["market", "source_id", "gate", "horizon", "decision_date"])
        .agg(ret=("ret", "mean"), excess=("excess", "mean"))
        .reset_index()
    )
    for keys, grp in allw.groupby(["market", "source_id", "gate", "horizon"]):
        exc = grp["excess"].to_numpy(dtype=float)
        ret = grp["ret"].to_numpy(dtype=float)
        st, ex = summarise(ret), summarise(exc)
        out.append(
            {
                "market": keys[0],
                "source_id": keys[1],
                "gate": keys[2],
                "horizon": keys[3],
                "window": "all",
                "dates": int(len(grp)),
                "mean_ret": st["mean"],
                "median_ret": st["median"],
                "mean_excess": ex["mean"],
                "median_excess": ex["median"],
                "dates_beating_market": ex["win_rate"],
                "excess_t_descriptive": ex["t_stat"],
            }
        )
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="research/screener_bakeoff/artifacts")
    args = ap.parse_args()
    d = pathlib.Path(args.dir)
    df = pd.read_csv(d / "picks.csv")
    scorecard, enriched = build(df)
    scorecard.to_csv(d / "scorecard.csv", index=False)
    date_level(enriched).to_csv(d / "scorecard_datelevel.csv", index=False)
    enriched.to_csv(d / "picks_scored.csv", index=False)
    print(f"scorecard rows: {len(scorecard)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
