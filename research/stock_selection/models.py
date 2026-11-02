"""Frozen baseline orientations and a training-only LightGBM rank regression."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .dataset import MODEL_FEATURES

# Higher score always means the up list; lower means the down list.
# Low volatility is the predeclared orientation of the volatility baseline.
BASELINES = {f: -1.0 if f.endswith("volatility_20") else 1.0 for f in MODEL_FEATURES}
LGBM_CONFIG = {
    "objective": "regression",
    "n_estimators": 60,
    "num_leaves": 7,
    "learning_rate": 0.05,
    "min_child_samples": 40,
    "reg_lambda": 1.0,
    "random_state": 1474,
    "n_jobs": 1,
    "verbosity": -1,
    "deterministic": True,
    "force_col_wise": True,
}


def ranker_predict(train: pd.DataFrame, test: pd.DataFrame, horizon: int) -> np.ndarray:
    from lightgbm import LGBMRegressor

    target = f"relative_return_{horizon}"
    train = train[train[target].notna()].copy()
    # These are training-day cross-sectional ranks; validation labels never fit
    # a transform or choose hyperparameters. No search/early stopping is run.
    train["rank_target"] = train.groupby("date")[target].rank(
        pct=True, method="average"
    )
    if len(train) < 40 or train.date.nunique() < 10:
        raise ValueError("insufficient matured training labels")
    model = LGBMRegressor(**LGBM_CONFIG)
    model.fit(train.loc[:, list(MODEL_FEATURES)], train.rank_target)
    return model.predict(test.loc[:, list(MODEL_FEATURES)])
