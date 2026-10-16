"""Q4 (rev2) -- rejected-forecast hit rate vs an *ex-ante* market baseline.

Original rule (``window-touch-v1-high-gte-low-lte``): over the daily bars of
the scored window the forecast hits when ``high >= target`` (at_or_above) or
``low <= target`` (at_or_below). Window authority: the resolver's own
``resolution_detail.window_start/window_end``; fallback
``forecast_start_date/review_date`` (``window_source`` records which).

* ``B`` = decision-time price (``core.point_in_time``); ``r = target/B - 1``.
* Baseline: the benchmark's (^KS11 / SPY / KRW-BTC) last 60 daily log returns
  whose bars closed **at or before** ``created_at`` give ``mu`` and ``sigma``;
  the expected benchmark return after ``t`` sessions is
  ``exp((mu + sigma^2/2) t) - 1`` (``core.expected_bench_return_pct``). No bar
  after the decision enters the baseline.
* ``t`` for a window bar = number of the symbol's sessions closing after
  ``created_at`` up to and including that bar.

Measures: ``recorded`` (stored outcome), ``raw_touch`` (rule replayed on public
bars -- replication), ``excess_touch`` (some window day: stock path return -
E[bench](t) crosses ``r``), ``excess_end`` (last close only).

Aggregation: one vote per parent episode (mean hit and mean probability of its
records); record-level rows are kept for reference. Gap = hit rate - mean
stated probability. LOSO / LOWO drop one symbol / one ISO week (KST, of the
episode's first record) at a time and report whether the gap sign survives.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.core import (
    decision_index,
    excess_hit,
    expected_bench_return_pct,
    point_in_time,
)
from research.rob1383_buy_drought.inputs import Rejection
from research.rob1383_buy_drought.prices import KST, PriceStore
from research.rob1383_buy_drought.stats import bootstrap_mean_ci, proportion_row

BASELINE_LOOKBACK = 60
MEASURES = ("recorded", "raw_touch", "excess_touch", "excess_end")


def bench_log_returns_before(
    bench: pd.DataFrame, at: Any, n: int
) -> list[float] | None:
    """Last ``n`` daily log returns of bars that closed at or before ``at``."""

    known = bench[bench["close_ts"] <= pd.Timestamp(at)]["close"].to_numpy(float)
    if len(known) < n + 1:
        return None
    return list(np.diff(np.log(known[-(n + 1) :])))


def run(
    rejections: Iterable[Rejection], store: PriceStore, episodes: dict[int, str]
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for rej in rejections:
        base = {
            "row": rej.row,
            "market": rej.market,
            "symbol": rej.symbol,
            "parent_episode_id": episodes.get(rej.row),
            "created_at_utc": rej.created_at.isoformat(),
            "week_kst": rej.created_at.astimezone(KST).strftime("%G-W%V"),
            "direction": rej.direction,
            "probability": rej.probability,
            "recorded": rej.outcome,
            "status": rej.status,
        }
        if rej.target_kind != "price_target" or rej.direction not in (
            "at_or_above",
            "at_or_below",
        ):
            records.append({**base, "excluded": f"target_kind:{rej.target_kind}"})
            continue
        if rej.status != "closed" or rej.outcome is None:
            records.append({**base, "excluded": f"not_scored:{rej.status}"})
            continue
        if rej.probability is None:
            records.append({**base, "excluded": "no_probability"})
            continue
        bars = store.bars(rej.market, rej.symbol)
        bench = store.benchmark(rej.market)
        if bars is None:
            reason = store.missing.get((rej.market, rej.symbol))
            records.append({**base, "excluded": f"price:{reason}"})
            continue
        if bench is None:
            records.append({**base, "excluded": "benchmark_missing"})
            continue
        splits = store.split_events.get((rej.market, rej.symbol), [])
        if any(d > str(rej.created_at.date()) for d in splits):
            records.append({**base, "excluded": "split_after_decision"})
            continue
        pit = point_in_time(bars, store.hourly(rej.market, rej.symbol), rej.created_at)
        pos = decision_index(bars, rej.created_at)
        if rej.window_start is not None and rej.window_end is not None:
            w_start, w_end, w_src = (
                rej.window_start,
                rej.window_end,
                "resolution_detail",
            )
        else:
            w_start, w_end, w_src = (
                rej.forecast_start,
                rej.review_date,
                "forecast_fields",
            )
        if pit is None or pos is None:
            records.append({**base, "excluded": "no_decision_bar"})
            continue
        if w_start is None or w_end is None:
            records.append({**base, "excluded": "no_window"})
            continue
        if bars["session_date"].iloc[-1] < w_end:
            records.append({**base, "excluded": "window_not_matured_in_data"})
            continue
        in_window = (bars["session_date"] >= w_start) & (bars["session_date"] <= w_end)
        window = bars[in_window]
        if window.empty:
            records.append({**base, "excluded": "empty_window"})
            continue
        past = bench_log_returns_before(bench, rej.created_at, BASELINE_LOOKBACK)
        if past is None:
            records.append({**base, "excluded": "benchmark_history_short"})
            continue
        b = float(pit.history["close"].iloc[-1])
        t_sessions = [int(p) - pos for p in np.flatnonzero(in_window.to_numpy())]
        expected = [expected_bench_return_pct(past, max(t, 1)) for t in t_sessions]
        synthetic = [1.0 + e / 100.0 for e in expected]
        kw: dict[str, Any] = {
            "direction": rej.direction,
            "base": b,
            "target": float(rej.target_price),
            "highs": window["high"].tolist(),
            "lows": window["low"].tolist(),
            "closes": window["close"].tolist(),
            "bench_base": 1.0,
        }
        records.append(
            {
                **base,
                "excluded": None,
                "base_price": b,
                "price_basis": pit.price_basis,
                "window_source": w_src,
                "window_start": str(w_start),
                "window_end": str(w_end),
                "target_price": rej.target_price,
                "required_move_pct": (float(rej.target_price) / b - 1.0) * 100.0,
                "window_bars": len(window),
                "t_last": t_sessions[-1],
                "bench_expected_pct_at_end": expected[-1],
                "raw_touch": excess_hit(
                    **kw, bench_closes=[1.0] * len(window), mode="touch"
                ),
                "excess_touch": excess_hit(**kw, bench_closes=synthetic, mode="touch"),
                "excess_end": excess_hit(**kw, bench_closes=synthetic, mode="end"),
            }
        )
    return pd.DataFrame.from_records(records)


def episode_table(scored: pd.DataFrame) -> pd.DataFrame:
    """One row per parent episode: mean hit per measure, mean probability."""

    frame = scored.copy()
    for m in MEASURES:
        frame[m] = frame[m].astype(bool).astype(float)
    out = frame.groupby("parent_episode_id").agg(
        symbol=("symbol", "first"),
        week_kst=("week_kst", "first"),
        n_records=("row", "size"),
        probability=("probability", "mean"),
        **{m: (m, "mean") for m in MEASURES},
    )
    return out.reset_index()


def _gap_stats(ep: pd.DataFrame, measure: str) -> dict[str, float]:
    hit = ep[measure].to_numpy(float)
    prob = ep["probability"].to_numpy(float)
    lo, hi = bootstrap_mean_ci(hit)
    glo, ghi = bootstrap_mean_ci(hit - prob)
    return {
        "hit_rate": float(hit.mean()),
        "hit_lo": lo,
        "hit_hi": hi,
        "mean_probability": float(prob.mean()),
        "gap_hit_minus_prob": float((hit - prob).mean()),
        "gap_lo": glo,
        "gap_hi": ghi,
    }


def summarise(detail: pd.DataFrame, market: str) -> pd.DataFrame:
    m = detail[detail["market"] == market]
    rows = []
    for direction in ("all", "at_or_above", "at_or_below"):
        src = m if direction == "all" else m[m["direction"] == direction]
        scored = src[src["excluded"].isna()]
        ep = episode_table(scored) if len(scored) else pd.DataFrame()
        common = {
            "market": market,
            "direction": direction,
            "n_rows": len(src),
            "n_excluded": int(src["excluded"].notna().sum()),
        }
        for measure in MEASURES:
            if scored.empty:
                rows.append({**common, "level": "record", "measure": measure, "n": 0})
                rows.append({**common, "level": "episode", "measure": measure, "n": 0})
                continue
            rec = proportion_row(
                scored[measure].astype(bool).tolist(), scored["symbol"].tolist()
            )
            prob = float(scored["probability"].mean())
            rows.append(
                {
                    **common,
                    "level": "record",
                    "measure": measure,
                    "n": rec["n"],
                    "n_symbols": scored["symbol"].nunique(),
                    "hit_rate": rec["rate"],
                    "hit_lo": rec["wilson_lo"],
                    "hit_hi": rec["wilson_hi"],
                    "ci_method": "wilson; symbol-cluster bootstrap in cluster_lo/hi",
                    "cluster_lo": rec.get("cluster_lo"),
                    "cluster_hi": rec.get("cluster_hi"),
                    "mean_probability": prob,
                    "gap_hit_minus_prob": rec["rate"] - prob,
                    "replication_agree_rate": float(
                        (
                            scored["raw_touch"].astype(bool)
                            == scored["recorded"].astype(bool)
                        ).mean()
                    ),
                }
            )
            rows.append(
                {
                    **common,
                    "level": "episode",
                    "measure": measure,
                    "n": len(ep),
                    "n_symbols": ep["symbol"].nunique(),
                    "ci_method": "percentile bootstrap over episodes",
                    **_gap_stats(ep, measure),
                }
            )
    return pd.DataFrame(rows)


def robustness(detail: pd.DataFrame, market: str) -> pd.DataFrame:
    scored = detail[(detail["market"] == market) & detail["excluded"].isna()]
    rows = []
    for direction in ("all", "at_or_above", "at_or_below"):
        src = scored if direction == "all" else scored[scored["direction"] == direction]
        if src.empty:
            continue
        ep = episode_table(src)
        for measure in MEASURES:
            full = float((ep[measure] - ep["probability"]).mean())
            for fold, col in (
                ("leave_one_symbol_out", "symbol"),
                ("leave_one_week_out", "week_kst"),
            ):
                gaps = []
                for key in sorted(ep[col].unique()):
                    rest = ep[ep[col] != key]
                    if len(rest):
                        gaps.append(float((rest[measure] - rest["probability"]).mean()))
                same = [np.sign(g) == np.sign(full) for g in gaps]
                rows.append(
                    {
                        "market": market,
                        "direction": direction,
                        "measure": measure,
                        "fold": fold,
                        "n_episodes": len(ep),
                        "n_folds": len(gaps),
                        "full_gap": full,
                        "full_gap_sign": int(np.sign(full)),
                        "share_folds_same_sign": float(np.mean(same))
                        if same
                        else np.nan,
                        "min_fold_gap": min(gaps) if gaps else np.nan,
                        "max_fold_gap": max(gaps) if gaps else np.nan,
                        "sign_robust_all_folds": bool(all(same)) if same else False,
                    }
                )
    return pd.DataFrame(rows)


def exclusions(detail: pd.DataFrame) -> pd.DataFrame:
    ex = detail[detail["excluded"].notna()]
    return ex.groupby(["market", "excluded"]).size().reset_index(name="n")
