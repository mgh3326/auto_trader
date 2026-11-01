"""Manual CLI: ``validate``, then ``diagnostic``, then ``report``. No scheduler.

``validate`` reads through 2022 only, fits the nested walk-forward, writes the
gate verdict and freezes the models. ``diagnostic`` is the single path that
reads 2023-2024: it refuses to run without the frozen bundle and never refits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from research.eval_contract.contract import BRACKET_CONSERVATIVE, BRACKET_OPTIMISTIC
from research.eval_contract.fill_models import ConservativeThrough
from research.kr_boosting import config as cfg
from research.kr_boosting import gate
from research.kr_boosting import pipeline as pl
from research.kr_boosting import policies as pol
from research.kr_boosting import train as tr
from research.kr_boosting.eligibility import d3_signals, decidable_signals
from research.kr_boosting.features import compute_features
from research.kr_boosting.panel import (
    PARQUET_NAME,
    ExportIdentity,
    Panel,
    build_panel,
    read_rows,
    verify_export,
)
from research.kr_boosting.simulator import SimOutput, session_slice, simulate

SEAL_BOUNDARY = cfg.TEST_START
VALIDATE_LAST = date(2022, 12, 31)
DIAGNOSTIC_CLAIM = "diagnostic_claim"
DIAGNOSTIC_LAST = cfg.TEST_END
FROZEN_YEAR = cfg.TEST_START.year


def git_state(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    return {
        "head": run("rev-parse", "HEAD"),
        "clean": run("status", "--porcelain") == "",
    }


def registered(repo: Path) -> dict[str, dict[str, Any]]:
    registry = json.loads((repo / "research/experiments/registry.json").read_text())
    by_id = {h["id"]: h for h in registry["hypotheses"]}
    return {h: by_id[h] for h in (cfg.HYP_ADMIT, cfg.HYP_ENTRY)}


def _serial(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serial(v) for v in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_serial(payload), indent=1, sort_keys=True) + "\n")


def _bundle_sha(run: pl.FamilyRun) -> dict[str, Any]:
    return {
        "family": run.family,
        "config_indexes": list(run.frozen_config_indexes),
        "sha256": [[f.sha256 for f in finalist] for finalist in run.frozen],
    }


def _fold_table(run: pl.FamilyRun) -> list[dict[str, Any]]:
    return [
        {
            "family": r.family,
            "year": r.year,
            "rows_fit": r.rows_fit,
            "rows_inner": r.rows_inner,
            "rows_train": r.rows_train,
            "rows_validate": r.rows_validate,
            "finalists": list(r.finalists),
            "best_loss": min(r.losses),
            "worst_loss": max(r.losses),
            "configs_tried": len(r.losses),
            "fitted_sha256": [list(x) for x in r.fitted_sha256],
        }
        for r in run.records
    ]


def _eligible_by_year(panel: Panel, signals: Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in signals:
        year = str(panel.calendar[s.t].year)
        out[year] = out.get(year, 0) + 1
    return dict(sorted(out.items()))


def _policy_section(
    panel: Panel,
    plans: dict[str, pol.Plan],
    *,
    ticks: Any,
    hypothesis_id: str,
    first: int,
    last: int,
) -> dict[str, dict[str, SimOutput]]:
    return {
        name: pl._sim_all(
            panel,
            plan,
            ticks=ticks,
            hypothesis_id=hypothesis_id,
            first=first,
            last=last,
        )
        for name, plan in plans.items()
    }


def _verdict(
    hypothesis_id: str, outputs: dict[str, dict[str, SimOutput]]
) -> gate.Verdict:
    gbm = outputs[pl.POL_GBM]
    conservative = gbm[BRACKET_CONSERVATIVE]
    capital = float(cfg.CAPITAL)
    controls = {
        name: runs[BRACKET_CONSERVATIVE].nav
        for name, runs in outputs.items()
        if name != pl.POL_GBM
    }
    return gate.decide(
        hypothesis_id,
        capital=capital,
        gbm_conservative=conservative.nav,
        gbm_optimistic=gbm[BRACKET_OPTIMISTIC].nav,
        controls_conservative=controls,
        unchanged=pl.unchanged_policy(hypothesis_id),
        years=[d.year for d in conservative.sessions],
    )


def _seed_runs(
    panel: Panel,
    make_plan: Any,
    per_seed: Sequence[Any],
    *,
    ticks: Any,
    hypothesis_id: str,
    first: int,
    last: int,
) -> list[float]:
    """Conservative annual net vs cash per seed of the primary configuration."""

    out: list[float] = []
    for pred in per_seed:
        run = simulate(
            panel,
            make_plan(pred),
            ConservativeThrough(),
            ticks=ticks,
            hypothesis_id=hypothesis_id,
            first_session=first,
            last_session=last,
        )
        out.append(pl.metrics(run)["net_vs_cash_annual"])
    return out


def validate(
    panel: Panel,
    *,
    root: Path,
    identity: ExportIdentity | None,
    years: Sequence[int] = cfg.VALIDATION_YEARS,
    seal_boundary: date = SEAL_BOUNDARY,
    frozen_year: int = FROZEN_YEAR,
    grid: Sequence[Any] = cfg.CONFIG_GRID,
    seeds: Sequence[int] = cfg.SEEDS,
    repo: Path | None = None,
) -> dict[str, Any]:
    ticks = pol.krx_ticks()
    store = compute_features(panel)
    signals = decidable_signals(panel)
    start = date(years[0], 1, 1)
    results: dict[str, Any] = {
        "stage": "validate",
        "at": datetime.now(UTC).isoformat(),
        "git": git_state(repo) if repo else None,
        "registry": {h: e["manifest_sha256"] for h, e in registered(repo).items()}
        if repo
        else None,
        "export": None if identity is None else vars_of(identity),
        "last_session_read": panel.calendar[-1],
        "eligible_by_year": _eligible_by_year(panel, signals),
        "grid_sha256": cfg.grid_sha256(),
        "seeds": list(seeds),
    }
    frozen: dict[str, Any] = {}
    outputs_all: dict[str, Any] = {}

    # ------------------------------------------------------------- H-admit
    purge, gap = pl.split_for(cfg.HYP_ADMIT)
    admit = pl.build_admit_rows(panel, store, signals)
    run = pl.run_family(
        "admit_quantile",
        calendar=panel.calendar,
        t=admit.t,
        x=admit.x,
        y=admit.y,
        predict_rows=None,
        predict_t=None,
        purge=purge,
        gap=gap,
        years=years,
        frozen_year=frozen_year,
        grid=grid,
        seeds=seeds,
        cache_dir=root / "fit_cache",
    )
    frozen["admit_quantile"] = run
    first_t, last_t = pl.decision_window(
        panel.calendar, start, seal_boundary, purge=purge, gap=gap
    )
    first, last = first_t, len(panel.calendar) - 1
    plans = pl.admit_plans(
        panel, store, ticks, admit, run.primary, first_t=first_t, last_t=last_t
    )
    outputs = _policy_section(
        panel, plans, ticks=ticks, hypothesis_id=cfg.HYP_ADMIT, first=first, last=last
    )
    outputs_all[cfg.HYP_ADMIT] = outputs
    verdict = _verdict(cfg.HYP_ADMIT, outputs)
    in_window = (admit.t >= first_t) & (admit.t <= last_t)

    def admit_plan(pred: np.ndarray) -> pol.Plan:
        return pl.admit_plans(
            panel, store, ticks, admit, pred, first_t=first_t, last_t=last_t
        )[pl.POL_GBM]

    results[cfg.HYP_ADMIT] = {
        "verdict": verdict.as_dict(),
        "policies": _metric_table(outputs),
        "sensitivity": {
            name: pl.sensitivity(
                panel,
                plans[name],
                ticks=ticks,
                hypothesis_id=cfg.HYP_ADMIT,
                first=first,
                last=last,
            )
            for name in (pl.POL_GBM, pl.POL_UNCHANGED_ADMIT)
        },
        "calibration": pl.quantile_coverage(admit.y[in_window], run.primary[in_window]),
        "admit_rate": pl.admit_rate(plans[pl.POL_GBM], admit.signals, first_t, last_t),
        "seed_net_vs_cash_annual": _seed_runs(
            panel,
            admit_plan,
            run.per_seed,
            ticks=ticks,
            hypothesis_id=cfg.HYP_ADMIT,
            first=first,
            last=last,
        ),
        "finalist_net_vs_cash_annual": _seed_runs(
            panel,
            admit_plan,
            run.finalists,
            ticks=ticks,
            hypothesis_id=cfg.HYP_ADMIT,
            first=first,
            last=last,
        ),
        "folds": _fold_table(run),
        "rows": {
            "total": len(admit.y),
            "label_known": int(np.isfinite(admit.y).sum()),
            "excluded_no_available_at": admit.excluded_no_available_at,
        },
        "decision_window": [panel.calendar[first_t], panel.calendar[last_t]],
    }

    # ------------------------------------------------------- H-entry-price
    purge, gap = pl.split_for(cfg.HYP_ENTRY)
    entry = pl.build_entry_rows(panel, store, ticks, signals)
    touch_run = pl.run_family(
        "touch",
        calendar=panel.calendar,
        t=entry.t,
        x=entry.x,
        y=entry.touched,
        predict_rows=None,
        predict_t=None,
        purge=purge,
        gap=gap,
        years=years,
        frozen_year=frozen_year,
        grid=grid,
        seeds=seeds,
        cache_dir=root / "fit_cache",
    )
    xret_run = pl.run_family(
        "xret",
        calendar=panel.calendar,
        t=entry.t,
        x=entry.x,
        y=entry.xret,
        predict_rows=None,
        predict_t=None,
        purge=purge,
        gap=gap,
        years=years,
        frozen_year=frozen_year,
        grid=grid,
        seeds=seeds,
        cache_dir=root / "fit_cache",
    )
    frozen["touch"] = touch_run
    frozen["xret"] = xret_run
    first_t, last_t = pl.decision_window(
        panel.calendar, start, seal_boundary, purge=purge, gap=gap
    )
    first, last = first_t, len(panel.calendar) - 1
    plans = pl.entry_plans(
        panel,
        store,
        ticks,
        entry,
        touch_run.primary,
        xret_run.primary,
        first_t=first_t,
        last_t=last_t,
    )
    outputs = _policy_section(
        panel, plans, ticks=ticks, hypothesis_id=cfg.HYP_ENTRY, first=first, last=last
    )
    outputs_all[cfg.HYP_ENTRY] = outputs
    verdict = _verdict(cfg.HYP_ENTRY, outputs)
    in_window = (entry.t >= first_t) & (entry.t <= last_t)

    def entry_plan(pred: tuple[np.ndarray, np.ndarray]) -> pol.Plan:
        return pl.entry_plans(
            panel, store, ticks, entry, pred[0], pred[1], first_t=first_t, last_t=last_t
        )[pl.POL_GBM]

    results[cfg.HYP_ENTRY] = {
        "verdict": verdict.as_dict(),
        "policies": _metric_table(outputs),
        "sensitivity": {
            name: pl.sensitivity(
                panel,
                plans[name],
                ticks=ticks,
                hypothesis_id=cfg.HYP_ENTRY,
                first=first,
                last=last,
            )
            for name in (pl.POL_GBM, pl.POL_UNCHANGED_ENTRY)
        },
        "calibration": pl.reliability(
            entry.touched[in_window], touch_run.primary[in_window, 0]
        ),
        "seed_net_vs_cash_annual": _seed_runs(
            panel,
            entry_plan,
            list(zip(touch_run.per_seed, xret_run.per_seed, strict=True)),
            ticks=ticks,
            hypothesis_id=cfg.HYP_ENTRY,
            first=first,
            last=last,
        ),
        "finalist_net_vs_cash_annual": _seed_runs(
            panel,
            entry_plan,
            list(zip(touch_run.finalists, xret_run.finalists, strict=True)),
            ticks=ticks,
            hypothesis_id=cfg.HYP_ENTRY,
            first=first,
            last=last,
        ),
        "folds": _fold_table(touch_run) + _fold_table(xret_run),
        "rows": {
            "total": len(entry.ladder),
            "touch_known": int(np.isfinite(entry.touched).sum()),
            "xret_known": int(np.isfinite(entry.xret).sum()),
            "excluded_no_available_at": entry.excluded_no_available_at,
        },
        "decision_window": [panel.calendar[first_t], panel.calendar[last_t]],
    }

    results["frozen"] = {name: _bundle_sha(item) for name, item in frozen.items()}
    root.mkdir(parents=True, exist_ok=True)
    with (root / "frozen_models.pkl").open("wb") as handle:
        pickle.dump(
            {
                name: (item.frozen, item.frozen_config_indexes)
                for name, item in frozen.items()
            },
            handle,
        )
    results["frozen_bundle_sha256"] = hashlib.sha256(
        (root / "frozen_models.pkl").read_bytes()
    ).hexdigest()
    with (root / "validate_outputs.pkl").open("wb") as handle:
        pickle.dump(_strip(outputs_all), handle)
    _dump(root / "validate_results.json", results)
    return results


def vars_of(identity: ExportIdentity) -> dict[str, Any]:
    return {
        "parquet_sha256": identity.parquet_sha256,
        "manifest_sha256": identity.manifest_sha256,
        "rows": identity.rows,
    }


def _metric_table(outputs: dict[str, dict[str, SimOutput]]) -> dict[str, Any]:
    return {
        policy: {model: pl.metrics(run) for model, run in runs.items()}
        for policy, runs in outputs.items()
    }


def _strip(outputs: dict[str, Any]) -> dict[str, Any]:
    """Only contract results survive to the report stage."""

    return {
        hyp: {
            policy: {m: r.result for m, r in runs.items()}
            for policy, runs in policies.items()
        }
        for hyp, policies in outputs.items()
    }


def _frozen_predict(frozen: dict[str, Any], family: str, x: np.ndarray) -> np.ndarray:
    fitted = frozen[family][0][0]  # top finalist, every seed
    return np.mean([tr.predict(f, x) for f in fitted], axis=0)


def _require_diagnostic_ready(root: Path) -> str:
    """Refuse a diagnostic that may not run; returns the frozen bundle's hash."""

    bundle = root / "frozen_models.pkl"
    validated = root / "validate_results.json"
    if not bundle.is_file() or not validated.is_file():
        raise FileNotFoundError("diagnostic needs the frozen bundle from `validate`")
    if (root / "diagnostic_results.json").exists():
        raise FileExistsError(
            "2023-2024 was already read for this run; it is not reopened"
        )
    expected = json.loads(validated.read_text())["frozen_bundle_sha256"]
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != expected:
        raise ValueError("frozen bundle changed after validate")
    return expected


def claim_diagnostic(root: Path) -> None:
    """Check, then durably record the look, before any sealed row is read.

    A crash after this leaves the claim in place, so it cannot read again unnoticed.
    A rerun needs the claim removed by hand and another look counted in the registry.
    """

    _require_diagnostic_ready(root)
    try:
        fd = os.open(root / DIAGNOSTIC_CLAIM, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise FileExistsError(
            f"{DIAGNOSTIC_CLAIM} exists: 2023-2024 may already have been read for "
            "this run. Count another look before removing it."
        ) from exc
    try:
        os.write(fd, datetime.now(UTC).isoformat().encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def diagnostic(panel: Panel, *, root: Path, identity: ExportIdentity) -> dict[str, Any]:
    """Frozen models on 2023-2024, once. Metrics are diagnostics, not a sealed test."""

    expected = _require_diagnostic_ready(root)
    with (root / "frozen_models.pkl").open("rb") as handle:
        frozen = pickle.load(handle)

    ticks = pol.krx_ticks()
    store = compute_features(panel)
    first_s, last_s = session_slice(panel.calendar, cfg.TEST_START, cfg.TEST_END)
    # A decision on first_s - 1 would use a 2022 close and trade on 2023's open;
    # the decision window starts inside the period, as validation does.
    signals = [s for s in d3_signals(panel, first_t=first_s) if s.t < last_s]
    results: dict[str, Any] = {
        "stage": "diagnostic_only_not_a_sealed_test",
        "at": datetime.now(UTC).isoformat(),
        "export": vars_of(identity),
        "frozen_bundle_sha256": expected,
        "eligible_by_year": _eligible_by_year(panel, signals),
    }
    outputs_all: dict[str, Any] = {}

    purge, _ = pl.split_for(cfg.HYP_ADMIT)
    admit = pl.build_admit_rows(panel, store, signals)
    pred = _frozen_predict(frozen, "admit_quantile", admit.x)
    last_t = last_s - purge
    plans = pl.admit_plans(
        panel, store, ticks, admit, pred, first_t=first_s, last_t=last_t
    )
    outputs = _policy_section(
        panel,
        plans,
        ticks=ticks,
        hypothesis_id=cfg.HYP_ADMIT,
        first=first_s,
        last=last_s,
    )
    outputs_all[cfg.HYP_ADMIT] = outputs
    in_window = (admit.t >= first_s) & (admit.t <= last_t)
    results[cfg.HYP_ADMIT] = {
        "policies": _metric_table(outputs),
        "calibration": pl.quantile_coverage(admit.y[in_window], pred[in_window]),
        "admit_rate": pl.admit_rate(plans[pl.POL_GBM], admit.signals, first_s, last_t),
        "decision_window": [panel.calendar[first_s], panel.calendar[last_t]],
    }

    purge, _ = pl.split_for(cfg.HYP_ENTRY)
    entry = pl.build_entry_rows(panel, store, ticks, signals)
    p_touch = _frozen_predict(frozen, "touch", entry.x)
    xret = _frozen_predict(frozen, "xret", entry.x)
    last_t = last_s - purge
    plans = pl.entry_plans(
        panel, store, ticks, entry, p_touch, xret, first_t=first_s, last_t=last_t
    )
    outputs = _policy_section(
        panel,
        plans,
        ticks=ticks,
        hypothesis_id=cfg.HYP_ENTRY,
        first=first_s,
        last=last_s,
    )
    outputs_all[cfg.HYP_ENTRY] = outputs
    in_window = (entry.t >= first_s) & (entry.t <= last_t)
    results[cfg.HYP_ENTRY] = {
        "policies": _metric_table(outputs),
        "calibration": pl.reliability(entry.touched[in_window], p_touch[in_window, 0]),
        "decision_window": [panel.calendar[first_s], panel.calendar[last_t]],
    }
    with (root / "diagnostic_outputs.pkl").open("wb") as handle:
        pickle.dump(_strip(outputs_all), handle)
    _dump(root / "diagnostic_results.json", results)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("validate", "diagnostic", "report"))
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    identity = verify_export(args.export_dir)
    parquet = args.export_dir / PARQUET_NAME
    if args.stage == "validate":
        rows = read_rows(parquet, last_session=VALIDATE_LAST)
        validate(build_panel(rows), root=args.root, identity=identity, repo=args.repo)
        return 0
    if args.stage == "diagnostic":
        claim_diagnostic(args.root)
        rows = read_rows(parquet, last_session=DIAGNOSTIC_LAST, allow_sealed=True)
        diagnostic(build_panel(rows), root=args.root, identity=identity)
        return 0
    from research.kr_boosting.report import write_report  # noqa: PLC0415

    if args.report is None:
        parser.error("report needs --report PATH")
    write_report(
        root=args.root,
        repo=args.repo,
        out=args.report,
        identity=identity,
        export_dir=args.export_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
