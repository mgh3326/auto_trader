"""Random-screener null distribution for the bakeoff.

For each market x horizon we draw B random 10-name portfolios on every
decision date from exactly the liquidity-filtered universe the benchmark uses,
collapse each draw to a date-level mean excess return, then average across
**the source's own usable (non-censored) dates**.  That yields the sampling
distribution of "mean excess of a 10-name screener that has no skill" on the
same date mask the source was scored on (S3 date-match).

A circular moving-block bootstrap of those dates (block length = horizon)
is stored alongside so window-overlap can be read off the table (S4).

This still does NOT fully control overlapping windows — the report states
that limit.  Block results are the sensitivity, not a replacement proof.
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

from research.screener_bakeoff.spec import RANDOM_SEED, TOP_N

B = 2000


def _as_day(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series).dt.strftime("%Y-%m-%d")


def _circular_block_indices(
    n: int, block_len: int, draws: int, rng: np.random.Generator
) -> np.ndarray:
    """(draws, n) indices into a length-n circular series."""
    if n <= 0:
        return np.zeros((draws, 0), dtype=int)
    length = max(1, min(int(block_len), n))
    n_blocks = int(np.ceil(n / length))
    starts = rng.integers(0, n, size=(draws, n_blocks))
    offsets = np.arange(length)
    idx = (starts[..., None] + offsets) % n
    return idx.reshape(draws, n_blocks * length)[:, :n]


def run(artifacts: pathlib.Path, draws: int = B) -> pd.DataFrame:
    uni = pd.read_csv(artifacts / "universe_returns.csv")
    picks = pd.read_csv(artifacts / "picks_scored.csv")
    picks = picks[~picks["source_id"].str.endswith(".benchmark")]
    picks = picks[~picks["censored"].astype(bool)]
    uni["decision_date"] = _as_day(uni["decision_date"])
    picks["decision_date"] = _as_day(picks["decision_date"])

    rng = np.random.default_rng(RANDOM_SEED)
    # Per (market, horizon, date) iid 10-name excess draws, generated in
    # sorted-date order so the RNG stream is deterministic.
    date_draw: dict[tuple[str, int, str], np.ndarray] = {}
    for (market, horizon), grp in uni.groupby(["market", "horizon"]):
        for day, sub in sorted(grp.groupby("decision_date"), key=lambda item: item[0]):
            r = sub["ret"].to_numpy(dtype=float)
            r = r[~np.isnan(r)]
            if r.size < TOP_N:
                continue
            idx = rng.integers(0, r.size, size=(draws, TOP_N))
            date_draw[(str(market), int(horizon), str(day))] = (
                r[idx].mean(axis=1) - r.mean()
            )

    block_rng = np.random.default_rng(RANDOM_SEED + 1)

    rows = []
    daily = (
        picks.groupby(["market", "source_id", "gate", "horizon", "decision_date"])
        .agg(excess=("excess", "mean"))
        .reset_index()
    )
    for (market, sid, gate, horizon), grp in daily.groupby(
        ["market", "source_id", "gate", "horizon"]
    ):
        grp = grp.sort_values("decision_date")
        dates = [str(d) for d in grp["decision_date"].tolist()]
        stacked = []
        used_dates = []
        for day in dates:
            vec = date_draw.get((str(market), int(horizon), day))
            if vec is None:
                continue
            stacked.append(vec)
            used_dates.append(day)
        if stacked:
            mat = np.vstack(stacked)  # (n_dates, draws)
            dist = mat.mean(axis=0)
        else:
            mat = None
            dist = None

        obs_series = grp["excess"].to_numpy(dtype=float)
        obs = float(np.nanmean(obs_series))
        if dist is None or np.isnan(obs):
            pct = None
        else:
            pct = float((dist < obs).mean())

        block_pct = None
        block_p05 = None
        block_p95 = None
        src_lo = None
        src_hi = None
        crosses = None
        if mat is not None and mat.shape[0] > 0:
            n = mat.shape[0]
            bidx = _circular_block_indices(n, int(horizon), draws, block_rng)
            # block-null: same iid draw column, dates resampled
            cols = np.arange(draws)[:, None]
            block_dist = mat[bidx, cols].mean(axis=1)
            block_pct = float((block_dist < obs).mean())
            block_p05 = float(np.percentile(block_dist, 5))
            block_p95 = float(np.percentile(block_dist, 95))
            # source block CI on the observed date-level excess series
            # aligned to used_dates (drop dates that had no universe draw)
            exc_map = {
                str(d): float(x)
                for d, x in zip(grp["decision_date"], grp["excess"], strict=False)
            }
            aligned = np.array(
                [exc_map[d] for d in used_dates if d in exc_map], dtype=float
            )
            # When n <= block length the circular block is the whole series
            # and the CI collapses to a point — leave it empty (not informative).
            if (
                aligned.size == n
                and n > int(horizon)
                and np.isfinite(aligned).sum() >= 2
            ):
                src_means = np.nanmean(aligned[bidx], axis=1)
                src_lo = float(np.nanpercentile(src_means, 2.5))
                src_hi = float(np.nanpercentile(src_means, 97.5))
                if np.isfinite(src_lo) and np.isfinite(src_hi):
                    crosses = bool(src_lo <= 0.0 <= src_hi)

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
                "block_null_pctile": block_pct,
                "block_null_p05": block_p05,
                "block_null_p95": block_p95,
                "block_source_ci_lo": src_lo,
                "block_source_ci_hi": src_hi,
                "block_ci_crosses_zero": crosses,
                "beats_null_95_block": bool(
                    block_pct is not None and block_pct >= 0.95
                ),
                "loses_null_05_block": bool(
                    block_pct is not None and block_pct <= 0.05 and crosses is False
                ),
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
