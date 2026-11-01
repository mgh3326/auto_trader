"""LightGBM families with nested selection. Imports LightGBM lazily.

For a validation year ``Y`` the configuration is chosen on year ``Y-1`` (fit on
rows cut before ``Y-1``, purge and gap included), so ``Y`` plays no part in
choosing the model that predicts it. The top ``FINALISTS`` configurations are
refit with every seed on rows cut before ``Y``; the top one's seed mean is the
primary prediction, the others are reported as robustness only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from research.kr_boosting.config import CONFIG_GRID, FINALISTS, QUANTILES, SEEDS

NUM_THREADS = 4
FAMILIES = ("admit_quantile", "touch", "xret")


def _lgb() -> Any:
    import lightgbm  # noqa: PLC0415 - research environment only

    return lightgbm


def _params(
    cfg: Mapping[str, Any], seed: int, objective: str, alpha: float | None
) -> dict[str, Any]:
    params = {k: v for k, v in cfg.items() if k != "n_estimators"}
    params |= {
        "objective": objective,
        "seed": seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": NUM_THREADS,
        "verbose": -1,
    }
    if alpha is not None:
        params["alpha"] = alpha
    return params


@dataclass(frozen=True, slots=True)
class Fitted:
    model_strings: tuple[str, ...]  # one per target head

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        for text in self.model_strings:
            digest.update(text.encode())
        return digest.hexdigest()


def _heads(family: str) -> tuple[tuple[str, float | None], ...]:
    if family == "admit_quantile":
        return tuple(("quantile", q) for q in QUANTILES)
    if family == "touch":
        return (("binary", None),)
    if family == "xret":
        return (("regression", None),)
    raise ValueError(f"unknown family {family!r}")


def winsor_limits(y: np.ndarray) -> tuple[float, float]:
    """Fitted on training labels only (rules §5)."""

    return float(np.quantile(y, 0.01)), float(np.quantile(y, 0.99))


def _fit_key(
    family: str, cfg: Mapping[str, Any], seed: int, x: np.ndarray, y: np.ndarray
) -> str:
    """Everything a booster depends on, so a changed input can never hit."""

    digest = hashlib.sha256()
    digest.update(Path(__file__).read_bytes())
    digest.update(_lgb().__version__.encode())
    digest.update(repr((family, sorted(cfg.items()), seed, x.shape, y.shape)).encode())
    digest.update(x.tobytes())
    digest.update(y.tobytes())
    return digest.hexdigest()


def fit(
    family: str,
    cfg: Mapping[str, Any],
    seed: int,
    x: np.ndarray,
    y: np.ndarray,
    *,
    cache_dir: Path | None = None,
) -> Fitted:
    """Fit one family's heads; ``cache_dir`` lets a rerun skip identical fits."""

    started = time.monotonic()
    path = None
    if cache_dir is not None:
        path = cache_dir / f"{_fit_key(family, cfg, seed, x, y)}.json"
        if path.is_file():
            print(f"fit {family} rows={len(y)} cached", file=sys.stderr, flush=True)
            return Fitted(tuple(json.loads(path.read_text())))
    fitted = _fit(family, cfg, seed, x, y)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".{os.getpid()}.tmp")
        partial.write_text(json.dumps(list(fitted.model_strings)))
        partial.replace(path)
    print(
        f"fit {family} rows={len(y)} seed={seed} {time.monotonic() - started:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    return fitted


def _fit(
    family: str, cfg: Mapping[str, Any], seed: int, x: np.ndarray, y: np.ndarray
) -> Fitted:
    lgb = _lgb()
    low, high = (
        winsor_limits(y) if family in ("admit_quantile", "xret") else (None, None)
    )
    target = np.clip(y, low, high) if low is not None else y
    models: list[str] = []
    for objective, alpha in _heads(family):
        data = lgb.Dataset(x, label=target, free_raw_data=True)
        booster = lgb.train(
            _params(cfg, seed, objective, alpha),
            data,
            num_boost_round=int(cfg["n_estimators"]),
        )
        models.append(booster.model_to_string())
    return Fitted(tuple(models))


def predict(fitted: Fitted, x: np.ndarray) -> np.ndarray:
    """One column per head; quantile heads are sorted so they cannot cross."""

    lgb = _lgb()
    preds = [
        lgb.Booster(model_str=text, params={"verbose": -1}).predict(x)
        for text in fitted.model_strings
    ]
    out = np.column_stack(preds)
    return np.sort(out, axis=1) if out.shape[1] > 1 else out


def pinball(y: np.ndarray, pred: np.ndarray, q: float) -> float:
    e = y - pred
    return float(np.mean(np.maximum(q * e, (q - 1) * e)))


def loss(family: str, y: np.ndarray, pred: np.ndarray) -> float:
    if family == "admit_quantile":
        return float(
            np.mean([pinball(y, pred[:, i], q) for i, q in enumerate(QUANTILES)])
        )
    if family == "touch":
        p = np.clip(pred[:, 0], 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    if family == "xret":
        return float(np.mean((y - pred[:, 0]) ** 2))
    raise ValueError(family)


@dataclass(frozen=True, slots=True)
class Selection:
    family: str
    year: int
    losses: tuple[float, ...]  # one per CONFIG_GRID entry, inner validation
    ranked: tuple[int, ...]

    @property
    def finalists(self) -> tuple[int, ...]:
        return self.ranked[:FINALISTS]


def select_config(
    family: str,
    year: int,
    *,
    x_fit: np.ndarray,
    y_fit: np.ndarray,
    x_inner: np.ndarray,
    y_inner: np.ndarray,
    grid: Sequence[Mapping[str, Any]] = CONFIG_GRID,
    seed: int = SEEDS[0],
    cache_dir: Path | None = None,
) -> Selection:
    limits = winsor_limits(y_fit) if family in ("admit_quantile", "xret") else None
    y_eval = np.clip(y_inner, *limits) if limits else y_inner
    losses = [
        loss(
            family,
            y_eval,
            predict(
                fit(family, cfg, seed, x_fit, y_fit, cache_dir=cache_dir),
                x_inner,
            ),
        )
        for cfg in grid
    ]
    ranked = tuple(int(i) for i in np.argsort(np.asarray(losses), kind="stable"))
    return Selection(family, year, tuple(losses), ranked)


@dataclass(frozen=True, slots=True)
class FoldPrediction:
    """Mean over seeds per finalist, shape (rows, heads); index 0 is primary."""

    per_finalist: tuple[np.ndarray, ...]
    primary_per_seed: tuple[np.ndarray, ...]
    fitted_sha256: tuple[tuple[str, ...], ...]
    fitted: tuple[tuple[Fitted, ...], ...]

    @property
    def primary(self) -> np.ndarray:
        return self.per_finalist[0]


def fit_finalists(
    family: str,
    selection: Selection,
    *,
    x_train: np.ndarray,
    y_train: np.ndarray,
    predict_x: Callable[[Fitted], np.ndarray],
    grid: Sequence[Mapping[str, Any]] = CONFIG_GRID,
    seeds: Sequence[int] = SEEDS,
    cache_dir: Path | None = None,
) -> FoldPrediction:
    means: list[np.ndarray] = []
    primary_per_seed: tuple[np.ndarray, ...] = ()
    shas: list[tuple[str, ...]] = []
    fitted_all: list[tuple[Fitted, ...]] = []
    for index in selection.finalists:
        fitted = tuple(
            fit(family, grid[index], seed, x_train, y_train, cache_dir=cache_dir)
            for seed in seeds
        )
        per_seed = tuple(predict_x(f) for f in fitted)
        if not means:
            primary_per_seed = per_seed
        means.append(np.mean(per_seed, axis=0))
        shas.append(tuple(f.sha256 for f in fitted))
        fitted_all.append(fitted)
    return FoldPrediction(
        tuple(means), primary_per_seed, tuple(shas), tuple(fitted_all)
    )
