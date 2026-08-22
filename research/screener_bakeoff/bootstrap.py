"""Random-screener null distribution for the bakeoff.

For each market x horizon we draw B random 10-name portfolios on every
decision date from exactly the liquidity-filtered universe the benchmark uses,
collapse each draw to a date-level mean excess return, then average across
dates.  That yields the sampling distribution of "mean excess of a 10-name
screener that has no skill", against which every real source is percentile
ranked.

This controls for cross-sectional selection.  It does NOT control for the
overlap between consecutive decision windows — the report states that limit.
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

from research.screener_bakeoff.spec import RANDOM_SEED, TOP_N

B = 2000


def run(artifacts: pathlib.Path, draws: int = B) -> pd.DataFrame:
    uni = pd.read_csv(artifacts / "universe_returns.csv")
    picks = pd.read_csv(artifacts / "picks_scored.csv")
    picks = picks[~picks["source_id"].str.endswith(".benchmark")]
    picks = picks[~picks["censored"].astype(bool)]

    rng = np.random.default_rng(RANDOM_SEED)
    null: dict[tuple[str, int], np.ndarray] = {}
    for (market, horizon), grp in uni.groupby(["market", "horizon"]):
        per_date = []
        for _, sub in grp.groupby("decision_date"):
            r = sub["ret"].to_numpy(dtype=float)
            r = r[~np.isnan(r)]
            if r.size < TOP_N:
                continue
            idx = rng.integers(0, r.size, size=(draws, TOP_N))
            per_date.append(r[idx].mean(axis=1) - r.mean())
        if not per_date:
            continue
        null[(market, horizon)] = np.vstack(per_date).mean(axis=0)

    rows = []
    daily = (
        picks.groupby(["market", "source_id", "gate", "horizon", "decision_date"])
        .agg(excess=("excess", "mean"))
        .reset_index()
    )
    for (market, sid, gate, horizon), grp in daily.groupby(
        ["market", "source_id", "gate", "horizon"]
    ):
        dist = null.get((market, horizon))
        obs = float(np.nanmean(grp["excess"].to_numpy(dtype=float)))
        if dist is None or np.isnan(obs):
            pct = None
        else:
            pct = float((dist < obs).mean())
        rows.append(
            {
                "market": market,
                "source_id": sid,
                "gate": gate,
                "horizon": horizon,
                "dates": int(len(grp)),
                "mean_excess": obs,
                "null_pctile": pct,
                "null_p05": float(np.percentile(dist, 5)) if dist is not None else None,
                "null_p95": float(np.percentile(dist, 95))
                if dist is not None
                else None,
                "beats_null_95": bool(pct is not None and pct >= 0.95),
                "loses_null_05": bool(pct is not None and pct <= 0.05),
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="research/screener_bakeoff/artifacts")
    ap.add_argument("--draws", type=int, default=B)
    args = ap.parse_args()
    d = pathlib.Path(args.dir)
    out = run(d, args.draws)
    out.to_csv(d / "bootstrap_null.csv", index=False)
    print(f"bootstrap rows: {len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
