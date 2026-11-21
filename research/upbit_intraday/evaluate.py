"""Frozen evaluation, selection and verdict rules for upbit-intraday-v1 (hk 6035).

Pinned by sha256 in the experiment manifest together with ``rules.py``.

Statistics. The unit of resampling is the KST calendar day of the entry time
(signals on one day are correlated through the market). A confidence interval
is the percentile interval of a day-cluster bootstrap of the ratio
``sum(excess) / count`` (2000 resamples, fixed seed).

Selection (develop half only). Per family, among the grid configurations with
at least ``MIN_N_SELECT`` signals that have a control-B value at the primary
horizon (4 h, entry at the next open, base costs), take the largest mean
excess over control B; ties go to the larger N, then the larger X. A family
with no such configuration has no selection and its verdict is
``INSUFFICIENT_SAMPLE``.

Verdict (test half, selected configuration, primary horizon, base costs),
all of: at least ``MIN_N_VERDICT`` signals on at least ``MIN_DAYS_VERDICT``
days; the excess over control B (same time, other markets of the same
liquidity bucket) has a Bonferroni-adjusted (3 families) bootstrap lower bound
above zero; the excess over control A (same market, other times of the same KST
hour) is above zero; the mean net return itself is above zero. Otherwise
``NOT_SUPPORTED``; with too few signals ``INSUFFICIENT_SAMPLE``.
"""

from __future__ import annotations

import numpy as np

from . import rules as R

BOOTSTRAP_B = 2000
CI_LEVEL = 0.95
CI_LEVEL_BONFERRONI = 1 - 0.05 / 3
MIN_N_SELECT = 30
MIN_N_VERDICT = 30
MIN_DAYS_VERDICT = 10


def _num(x: float | np.floating | None) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


def cluster_bootstrap_ci(
    values: np.ndarray, days: np.ndarray, level: float, seed: int
) -> tuple[float | None, float | None]:
    """Percentile CI of mean(values) resampling whole days."""
    ok = np.isfinite(values)
    values, days = values[ok], days[ok]
    if len(values) == 0:
        return None, None
    uniq, inv = np.unique(days, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(uniq))
    cnts = np.bincount(inv, minlength=len(uniq)).astype(float)
    rng = np.random.default_rng(np.random.SeedSequence([R.SEED, seed]))
    draw = rng.integers(0, len(uniq), size=(BOOTSTRAP_B, len(uniq)))
    means = sums[draw].sum(axis=1) / cnts[draw].sum(axis=1)
    alpha = (1 - level) / 2
    lo, hi = np.quantile(means, [alpha, 1 - alpha])
    return float(lo), float(hi)


def _stat_block(
    prefix: str, excess: np.ndarray, days: np.ndarray, seed: int, with_ci: bool
) -> dict:
    ok = np.isfinite(excess)
    out = {
        f"n_{prefix}": int(ok.sum()),
        f"excess_{prefix}_mean": _num(excess[ok].mean()) if ok.any() else None,
    }
    if with_ci and ok.any():
        lo, hi = cluster_bootstrap_ci(excess, days, CI_LEVEL, seed)
        blo, _ = cluster_bootstrap_ci(excess, days, CI_LEVEL_BONFERRONI, seed + 1)
        out.update(
            {
                f"excess_{prefix}_ci95": [_num(lo), _num(hi)],
                f"excess_{prefix}_lo_bonferroni": _num(blo),
            }
        )
    return out


def summarize(
    ctx: R.Context,
    sset: R.SignalSet,
    half: str,
    h: int,
    delay: int,
    scenario: str,
    *,
    bucket: int | None = None,
    with_ci: bool = True,
) -> dict:
    """Aggregate outcome of one configuration/half/horizon/delay/cost scenario."""
    cfg = sset.config
    sel = sset.half == half
    if bucket is not None:
        sel = sel & (sset.bucket == bucket)
    sel_idx = np.flatnonzero(sel)
    net = ctx.net(h, delay, cfg.direction, scenario)
    gross = cfg.direction * ctx.gross(h, delay)
    sig_net_all = net[sset.mi, sset.s0] if len(sset.s0) else np.asarray([])
    ctrl_b_all = R.control_means(net, sset.b_gid, sset.b_mi, sset.b_s, len(sset.s0))
    ctrl_a_all = R.control_means(net, sset.a_gid, sset.a_mi, sset.a_s, len(sset.s0))
    sig_net = sig_net_all[sel_idx]
    used = np.isfinite(sig_net)
    gross_sig = gross[sset.mi, sset.s0][sel_idx] if len(sset.s0) else np.asarray([])
    days = sset.day[sel_idx]
    mae = R.adverse_excursion(
        ctx.panel, sset.mi[sel_idx], sset.s0[sel_idx], h, delay, cfg.direction
    )
    seed_base = (
        R.FAMILY_CODE[cfg.family] * 10_000
        + cfg.n * 100
        + int(cfg.x * 10)
        + h * 7
        + delay
    )
    cb = ctrl_b_all[sel_idx]
    ca = ctrl_a_all[sel_idx]
    out: dict = {
        "n_signals": int(len(sel_idx)),
        "n_used": int(used.sum()),
        "n_days": int(len(np.unique(days[used]))) if used.any() else 0,
        "mean_gross": _num(gross_sig[used].mean()) if used.any() else None,
        "mean_net": _num(sig_net[used].mean()) if used.any() else None,
        "median_net": _num(np.median(sig_net[used])) if used.any() else None,
        "hit_rate": _num((sig_net[used] > 0).mean()) if used.any() else None,
        "mae_mean": _num(np.nanmean(mae)) if np.isfinite(mae).any() else None,
        "mae_p90": _num(np.nanquantile(mae, 0.9)) if np.isfinite(mae).any() else None,
        "worst_net": _num(sig_net[used].min()) if used.any() else None,
        "ctrl_b_mean_net": _num(np.nanmean(np.where(used, cb, np.nan)))
        if (used & np.isfinite(cb)).any()
        else None,
        "ctrl_a_mean_net": _num(np.nanmean(np.where(used, ca, np.nan)))
        if (used & np.isfinite(ca)).any()
        else None,
    }
    out.update(_stat_block("b", sig_net - cb, days, seed_base, with_ci))
    out.update(_stat_block("a", sig_net - ca, days, seed_base + 3, with_ci))
    return out


def evaluate_config(
    ctx: R.Context,
    sset: R.SignalSet,
    half: str,
    *,
    delays: tuple[int, ...] = (0,),
    scenarios: tuple[str, ...] = (R.PRIMARY_SCENARIO,),
    horizons: tuple[int, ...] = R.HORIZONS,
    by_bucket: bool = False,
    with_ci: bool = True,
) -> list[dict]:
    cfg = sset.config
    records: list[dict] = []
    for h in horizons:
        for delay in delays:
            for scenario in scenarios:
                for bucket in (None, *range(3)) if by_bucket else (None,):
                    rec = {
                        "half": half,
                        "family": cfg.family,
                        "config": cfg.id,
                        "n": cfg.n,
                        "x": cfg.x,
                        "h": h,
                        "delay": delay,
                        "scenario": scenario,
                        "bucket": None if bucket is None else R.BUCKET_NAMES[bucket],
                    }
                    rec.update(
                        summarize(
                            ctx,
                            sset,
                            half,
                            h,
                            delay,
                            scenario,
                            bucket=bucket,
                            with_ci=with_ci,
                        )
                    )
                    records.append(rec)
    return records


def select_config(dev_records: list[dict], family: str) -> dict | None:
    """Selection rule on develop-half records (primary horizon, delay 0, base)."""
    cands = [
        r
        for r in dev_records
        if r["family"] == family
        and r["h"] == R.PRIMARY_H
        and r["delay"] == 0
        and r["scenario"] == R.PRIMARY_SCENARIO
        and r["bucket"] is None
        and r["half"] == "dev"
        and (r.get("n_b") or 0) >= MIN_N_SELECT
        and r.get("excess_b_mean") is not None
    ]
    if not cands:
        return None
    best = max(cands, key=lambda r: (r["excess_b_mean"], r["n"], r["x"]))
    return {"family": family, "config": best["config"], "n": best["n"], "x": best["x"]}


def verdict(test_record: dict | None) -> dict:
    """Verdict for one family from its selected configuration's test record."""
    if test_record is None:
        return {
            "verdict": "INSUFFICIENT_SAMPLE",
            "reason": "no configuration reached the develop-half minimum",
        }
    n_used = test_record.get("n_used") or 0
    n_days = test_record.get("n_days") or 0
    if n_used < MIN_N_VERDICT or n_days < MIN_DAYS_VERDICT:
        return {
            "verdict": "INSUFFICIENT_SAMPLE",
            "reason": f"n_used={n_used} (need {MIN_N_VERDICT}), n_days={n_days} (need {MIN_DAYS_VERDICT})",
        }
    lo_b = test_record.get("excess_b_lo_bonferroni")
    ex_a = test_record.get("excess_a_mean")
    mean_net = test_record.get("mean_net")
    checks = {
        "excess_vs_control_b_lower_bound_above_zero": lo_b is not None and lo_b > 0,
        "excess_vs_control_a_above_zero": ex_a is not None and ex_a > 0,
        "mean_net_above_zero": mean_net is not None and mean_net > 0,
    }
    return {
        "verdict": "SUPPORTED" if all(checks.values()) else "NOT_SUPPORTED",
        "checks": checks,
    }
