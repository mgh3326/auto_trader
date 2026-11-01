"""Rows, nested training and policy runs, independent of files and the CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from research.eval_contract.fill_models import ConservativeThrough, FillModel
from research.kr_boosting import labels as lab
from research.kr_boosting import policies as pol
from research.kr_boosting import train as tr
from research.kr_boosting.config import (
    CONFIG_GRID,
    FEE_RATE,
    HYP_ADMIT,
    SENSITIVITY_FEE_RATE,
    SESSIONS_PER_YEAR,
    SPLITS,
    TRAIN_START,
)
from research.kr_boosting.eligibility import Signal
from research.kr_boosting.features import (
    FEATURE_NAMES,
    FeatureStore,
    feature_join,
)
from research.kr_boosting.gate import annualised, daily_returns, max_drawdown
from research.kr_boosting.panel import Panel
from research.kr_boosting.simulator import (
    WRITE_OFF_SEMANTICS,
    SimOutput,
    fill_models,
    session_slice,
    simulate,
)
from research.kr_boosting.splits import cut_before, last_decision_before
from research.kr_corpus.d3_engine.tick import TickTable

ADMIT_CONTEXT = ("ctx_rsi", "ctx_l2_distance")
ENTRY_CONTEXT = (*ADMIT_CONTEXT, "level_distance", "level_kind", "level_over_rv20")
ADMIT_FEATURES = (*FEATURE_NAMES, *ADMIT_CONTEXT)
ENTRY_FEATURES = (*FEATURE_NAMES, *ENTRY_CONTEXT)

POL_GBM = "gbm-baseline"
POL_UNCHANGED_ADMIT = "control-admit-all"
POL_UNCHANGED_ENTRY = "control-anchor"
POL_MOMENTUM = "control-momentum"
POL_REVERSION = "control-mean-reversion"
POL_CASH = "control-cash"


@dataclass(slots=True)
class AdmitRows:
    signals: list[Signal]
    t: np.ndarray
    x: np.ndarray
    y: np.ndarray
    excluded_no_available_at: int


@dataclass(slots=True)
class EntryRows:
    signals: list[Signal]
    ladder: list[pol.LadderRow]
    t: np.ndarray
    x: np.ndarray
    touched: np.ndarray
    xret: np.ndarray
    excluded_no_available_at: int


def _base_matrix(
    panel: Panel, store: FeatureStore, signals: Sequence[Signal]
) -> tuple[np.ndarray, np.ndarray, list[int], int]:
    """Feature matrix per signal through the causal join; keeps only joined rows."""

    li = np.array([s.li for s in signals], dtype=np.int64)
    t = np.array([s.t for s in signals], dtype=np.int64)
    joined = feature_join(panel, store, li, t)
    kept = {
        (int(a), int(b))
        for a, b in zip(joined.frame["li"], joined.frame["t"], strict=True)
    }
    keep = [i for i, s in enumerate(signals) if (s.li, s.t) in kept]
    matrix = joined.frame[list(FEATURE_NAMES)].to_numpy(dtype=np.float32)
    return matrix, t[keep], keep, joined.excluded_no_available_at


def build_admit_rows(
    panel: Panel, store: FeatureStore, signals: Sequence[Signal]
) -> AdmitRows:
    matrix, t, keep, excluded = _base_matrix(panel, store, signals)
    kept = [signals[i] for i in keep]
    context = np.array(
        [[s.rsi, s.l2_distance] for s in kept], dtype=np.float32
    ).reshape(-1, 2)
    y = np.array([lab.admit_label(panel, s.li, s.t) for s in kept], dtype=np.float64)
    return AdmitRows(kept, t, np.hstack([matrix, context]), y, excluded)


def build_entry_rows(
    panel: Panel, store: FeatureStore, ticks: TickTable, signals: Sequence[Signal]
) -> EntryRows:
    matrix, _, keep, excluded = _base_matrix(panel, store, signals)
    kept = [signals[i] for i in keep]
    rows: list[pol.LadderRow] = []
    source: list[int] = []
    extra: list[list[float]] = []
    touched: list[float] = []
    xret: list[float] = []
    t: list[int] = []
    market = store.market["mkt_ret_1"]
    rv_col = store.arrays["rv_20"]
    for index, s in enumerate(kept):
        close = float(panel.close[s.li, s.t])
        rv = rv_col[s.li, s.t]
        market_ret = market[int(panel.is_kosdaq[s.li])]
        for kind, price in pol.ladder(panel, ticks, s):
            level = float(price)
            distance = level / close - 1.0
            touch = lab.touch_label(panel, s.li, s.t, level)
            rows.append(pol.LadderRow(index, kind, price))
            source.append(index)
            t.append(s.t)
            extra.append(
                [
                    s.rsi,
                    s.l2_distance,
                    distance,
                    float(pol.LEVEL_KINDS.index(kind)),
                    distance / rv if rv and rv == rv else np.nan,
                ]
            )
            touched.append(touch.touched)
            xret.append(lab.xret_label(panel, s.li, touch, market_ret))
    x = np.hstack(
        [
            matrix[source] if source else matrix[:0],
            np.array(extra, dtype=np.float32).reshape(-1, len(ENTRY_CONTEXT)),
        ]
    )
    return EntryRows(
        kept,
        rows,
        np.array(t, dtype=np.int64),
        x,
        np.array(touched, dtype=np.float64),
        np.array(xret, dtype=np.float64),
        excluded,
    )


def _year_positions(calendar: Sequence[date], year: int) -> tuple[int, int]:
    return session_slice(calendar, date(year, 1, 1), date(year, 12, 31))


def _masks(
    calendar: Sequence[date], t: np.ndarray, year: int, *, purge: int, gap: int
) -> dict[str, np.ndarray]:
    """Row masks of one nested fold for validation (or frozen) year ``year``.

    ``fit``/``inner`` choose the configuration on year-1; ``train`` refits for
    ``year``. Each boundary has purge + gap in front of it, including inner→year.
    """

    outer = cut_before(calendar, date(year, 1, 1), purge=purge, gap=gap)
    inner = cut_before(calendar, date(year - 1, 1, 1), purge=purge, gap=gap)
    start = next(i for i, d in enumerate(calendar) if d >= TRAIN_START)
    inner_start = next(i for i, d in enumerate(calendar) if d >= date(year - 1, 1, 1))
    return {
        "fit": (t >= start) & (t <= inner.last_kept),
        "inner": (t >= inner_start) & (t <= outer.last_kept),
        "train": (t >= start) & (t <= outer.last_kept),
    }


@dataclass(slots=True)
class FoldRecord:
    family: str
    year: int
    rows_fit: int
    rows_inner: int
    rows_train: int
    rows_validate: int
    losses: tuple[float, ...]
    finalists: tuple[int, ...]
    fitted_sha256: tuple[tuple[str, ...], ...]


@dataclass(slots=True)
class FamilyRun:
    """Out-of-sample predictions on all rows; NaN outside a validation year."""

    family: str
    primary: np.ndarray
    finalists: list[np.ndarray]
    per_seed: list[np.ndarray]
    records: list[FoldRecord] = field(default_factory=list)
    frozen: tuple[tuple[tr.Fitted, ...], ...] = ()
    frozen_config_indexes: tuple[int, ...] = ()


def run_family(
    family: str,
    *,
    calendar: Sequence[date],
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    predict_rows: np.ndarray | None,
    predict_t: np.ndarray | None,
    purge: int,
    gap: int,
    years: Sequence[int],
    frozen_year: int,
    grid: Sequence[Mapping[str, Any]] = CONFIG_GRID,
    seeds: Sequence[int] = tr.SEEDS,
    cache_dir: Path | None = None,
) -> FamilyRun:
    """Nested walk-forward over ``years`` plus the frozen fit for ``frozen_year``.

    ``y`` may be NaN (unknown or not applicable); such rows never fit or score.
    ``predict_rows`` defaults to ``x`` when a family trains on a subset.
    """

    px = x if predict_rows is None else predict_rows
    pt = t if predict_t is None else predict_t
    heads = len(tr._heads(family))
    known = np.isfinite(y)
    out = FamilyRun(
        family,
        primary=np.full((len(px), heads), np.nan),
        finalists=[np.full((len(px), heads), np.nan) for _ in range(tr.FINALISTS)],
        per_seed=[np.full((len(px), heads), np.nan) for _ in seeds],
    )
    for year in [*years, frozen_year]:
        m = _masks(calendar, t, year, purge=purge, gap=gap)
        fit_mask, inner_mask, train_mask = (
            m[k] & known for k in ("fit", "inner", "train")
        )
        selection = tr.select_config(
            family,
            year,
            x_fit=x[fit_mask],
            y_fit=y[fit_mask],
            x_inner=x[inner_mask],
            y_inner=y[inner_mask],
            grid=grid,
            seed=seeds[0],
            cache_dir=cache_dir,
        )
        if year == frozen_year:
            target = np.zeros(0, dtype=bool)
        else:
            first, last = _year_positions(calendar, year)
            target = (pt >= first) & (pt <= last)
        fold = tr.fit_finalists(
            family,
            selection,
            x_train=x[train_mask],
            y_train=y[train_mask],
            predict_x=lambda fitted, rows=px[target]: tr.predict(fitted, rows),
            grid=grid,
            seeds=seeds,
            cache_dir=cache_dir,
        )
        out.records.append(
            FoldRecord(
                family,
                year,
                int(fit_mask.sum()),
                int(inner_mask.sum()),
                int(train_mask.sum()),
                int(target.sum()),
                selection.losses,
                selection.finalists,
                fold.fitted_sha256,
            )
        )
        if year == frozen_year:
            out.frozen = fold.fitted
            out.frozen_config_indexes = selection.finalists
            continue
        out.primary[target] = fold.primary
        for i, pred in enumerate(fold.per_finalist):
            out.finalists[i][target] = pred
        for i, pred in enumerate(fold.primary_per_seed):
            out.per_seed[i][target] = pred
    return out


# ------------------------------------------------------------- policy runs


@dataclass(slots=True)
class PolicyRun:
    policy: str
    outputs: dict[str, SimOutput]


def _sim_all(
    panel: Panel,
    plan: pol.Plan,
    *,
    ticks: TickTable,
    hypothesis_id: str,
    first: int,
    last: int,
    models: Sequence[FillModel] | None = None,
) -> dict[str, SimOutput]:
    return {
        m.model_id: simulate(
            panel,
            plan,
            m,
            ticks=ticks,
            hypothesis_id=hypothesis_id,
            first_session=first,
            last_session=last,
        )
        for m in (models if models is not None else fill_models())
    }


def in_window(signals: Sequence[Signal], first_t: int, last_t: int) -> list[int]:
    return [i for i, s in enumerate(signals) if first_t <= s.t <= last_t]


def admit_plans(
    panel: Panel,
    store: FeatureStore,
    ticks: TickTable,
    rows: AdmitRows,
    pred: np.ndarray,
    *,
    first_t: int,
    last_t: int,
) -> dict[str, pol.Plan]:
    idx = in_window(rows.signals, first_t, last_t)
    signals = [rows.signals[i] for i in idx]
    sessions = list(range(first_t, last_t + 1))
    q = pred[idx]
    ok = np.isfinite(q).all(axis=1)
    if not ok.all():
        raise ValueError("a decision row in the window has no out-of-sample prediction")
    return {
        POL_GBM: pol.gbm_admit_plan(panel, ticks, signals, q[:, 0], q[:, 1]),
        POL_UNCHANGED_ADMIT: pol.admit_all_plan(panel, ticks, signals),
        POL_MOMENTUM: pol.momentum_plan(panel, ticks, store, sessions, "admit"),
        POL_REVERSION: pol.mean_reversion_plan(panel, ticks, store, sessions, "admit"),
        POL_CASH: {},
    }


def entry_plans(
    panel: Panel,
    store: FeatureStore,
    ticks: TickTable,
    rows: EntryRows,
    p_touch: np.ndarray,
    xret: np.ndarray,
    *,
    first_t: int,
    last_t: int,
) -> dict[str, pol.Plan]:
    mask = (rows.t >= first_t) & (rows.t <= last_t)
    idx = np.flatnonzero(mask)
    if not np.isfinite(p_touch[idx]).all() or not np.isfinite(xret[idx]).all():
        raise ValueError("a decision row in the window has no out-of-sample prediction")
    ladder_rows = [rows.ladder[i] for i in idx]
    keep = [i for i, s in enumerate(rows.signals) if first_t <= s.t <= last_t]
    sessions = list(range(first_t, last_t + 1))
    return {
        POL_GBM: pol.gbm_entry_plan(
            panel, ticks, rows.signals, ladder_rows, p_touch[idx, 0], xret[idx, 0]
        ),
        POL_UNCHANGED_ENTRY: pol.anchor_plan(
            panel, ticks, [rows.signals[i] for i in keep]
        ),
        POL_MOMENTUM: pol.momentum_plan(panel, ticks, store, sessions, "entry"),
        POL_REVERSION: pol.mean_reversion_plan(panel, ticks, store, sessions, "entry"),
        POL_CASH: {},
    }


def decision_window(
    calendar: Sequence[date], start: date, boundary: date, *, purge: int, gap: int
) -> tuple[int, int]:
    """First decision session and last one whose windows stay before ``boundary``."""

    last = last_decision_before(calendar, boundary, purge=purge, gap=gap)
    first = next(i for i, d in enumerate(calendar) if d >= start)
    return first, calendar.index(last)


def unchanged_policy(hypothesis_id: str) -> str:
    return POL_UNCHANGED_ADMIT if hypothesis_id == HYP_ADMIT else POL_UNCHANGED_ENTRY


def split_for(hypothesis_id: str) -> tuple[int, int]:
    spec = SPLITS[hypothesis_id]
    return spec.purge, spec.gap


# ----------------------------------------------------------------- metrics


def metrics(out: SimOutput) -> dict[str, Any]:
    r = out.result
    if r.status != "ok":
        return {"status": "unavailable", "reason": r.unavailable_reason}
    capital = float(r.capital or 0)
    returns = daily_returns(out.nav, capital)
    years = sorted({d.year for d in out.sessions})
    axis = np.array([d.year for d in out.sessions])
    span = max(len(out.sessions) / SESSIONS_PER_YEAR, 1e-9)
    mean_nav = float(out.nav.mean()) if len(out.nav) else capital
    at_5 = [o for o in r.outcomes if o.horizon == 5]
    at_20 = [o for o in r.outcomes if o.horizon == 20]

    def mean(items: Sequence[Any]) -> float | None:
        seen = [float(o.net_return) for o in items if not o.censored]
        return float(np.mean(seen)) if seen else None

    return {
        "status": "ok",
        "net_vs_cash_total": float(r.net_vs_cash or 0),
        "net_vs_cash_annual": annualised(returns),
        "by_year": {str(y): float(returns[axis == y].sum()) for y in years},
        "terminal_value": str(r.terminal_value),
        "max_drawdown": max_drawdown(out.nav, capital),
        "turnover_annual": float(out.traded_gross) / 2.0 / mean_nav / span,
        "deployed_mean": float(out.deployed.mean()) if len(out.deployed) else 0.0,
        "orders": len(r.orders),
        "filled_orders": len(r.filled_order_ids),
        "fill_rate": float(r.fill_rate) if r.fill_rate is not None else None,
        "entries": sum(f.side.value == "buy" for f in r.fills),
        "exits": sum(f.side.value == "sell" for f in r.fills),
        "open_at_end": out.unresolved_at_end,
        "terminal_writeoffs": out.writeoffs,
        "mean_net_r5": mean(at_5),
        "mean_net_r20": mean(at_20),
        "censored_r20": sum(o.censored for o in at_20),
    }


def sensitivity(
    panel: Panel,
    plan: pol.Plan,
    *,
    ticks: TickTable,
    hypothesis_id: str,
    first: int,
    last: int,
) -> dict[str, dict[str, Any]]:
    """Conservative end under stress: higher cost; terminal write-off."""

    conservative = ConservativeThrough()
    return {
        "fee_0.415pct_per_side": metrics(
            simulate(
                panel,
                plan,
                conservative,
                ticks=ticks,
                hypothesis_id=hypothesis_id,
                first_session=first,
                last_session=last,
                fee_rate=SENSITIVITY_FEE_RATE,
            )
        ),
        "terminal_writeoff": metrics(
            simulate(
                panel,
                plan,
                conservative,
                ticks=ticks,
                hypothesis_id=hypothesis_id,
                first_session=first,
                last_session=last,
                fee_rate=FEE_RATE,
                terminal_writeoff=WRITE_OFF_SEMANTICS,
            )
        ),
    }


# ------------------------------------------------------------- calibration


def quantile_coverage(y: np.ndarray, q: np.ndarray) -> dict[str, Any]:
    ok = np.isfinite(y) & np.isfinite(q).all(axis=1)
    if not ok.any():
        return {"n": 0}
    yy, qq = y[ok], q[ok]
    return {
        "n": int(ok.sum()),
        "below_q10": float((yy < qq[:, 0]).mean()),
        "below_q50": float((yy < qq[:, 1]).mean()),
        "below_q90": float((yy < qq[:, 2]).mean()),
        "pinball_q50": tr.pinball(yy, qq[:, 1], 0.5),
    }


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 5) -> dict[str, Any]:
    ok = np.isfinite(y) & np.isfinite(p)
    if not ok.any():
        return {"n": 0, "bins": []}
    yy, pp = y[ok], p[ok]
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for a, b in zip(edges[:-1], edges[1:], strict=True):
        m = (pp >= a) & (pp < b if b < 1 else pp <= b)
        if m.any():
            rows.append(
                {
                    "bin": f"{a:.1f}-{b:.1f}",
                    "n": int(m.sum()),
                    "predicted": float(pp[m].mean()),
                    "observed": float(yy[m].mean()),
                }
            )
    return {"n": int(ok.sum()), "base_rate": float(yy.mean()), "bins": rows}


def admit_rate(
    plan: pol.Plan, signals: Sequence[Signal], first_t: int, last_t: int
) -> float:
    eligible = sum(first_t <= s.t <= last_t for s in signals)
    admitted = sum(len(v) for t, v in plan.items() if first_t <= t <= last_t)
    return admitted / eligible if eligible else 0.0
