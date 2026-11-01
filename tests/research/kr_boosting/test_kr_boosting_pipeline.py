"""End-to-end on synthetic bars. Each test skips without LightGBM, so CI skips them.

Run in the research environment:

    PYTHONPATH=. "$ROOT/venv/bin/python" -m pytest --noconftest -c /dev/null \
      tests/research/kr_boosting/test_kr_boosting_pipeline.py
"""

from __future__ import annotations

import json
import pickle
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from research.eval_contract.contract import BRACKET_CONSERVATIVE
from research.kr_boosting import pipeline as pl
from research.kr_boosting import run
from research.kr_boosting.config import CONFIG_GRID, HYP_ADMIT, HYP_ENTRY
from research.kr_boosting.panel import build_panel
from tests.research.kr_boosting.helpers import (
    frame,
    random_walk_rows,
    weekdays,
)

GRID = tuple({**c, "n_estimators": 5, "min_data_in_leaf": 5} for c in CONFIG_GRID[:3])
SEEDS = (1, 2)


@pytest.fixture(scope="module")
def panel():
    pytest.importorskip("lightgbm")
    sessions = weekdays(date(2015, 1, 1), 1150)  # through 2019
    rows: list[dict[str, object]] = []
    for i in range(14):
        rows += random_walk_rows(f"0000{i:02d}", sessions, 100 + i)
    return build_panel(frame(rows))


@pytest.fixture(scope="module")
def result(panel, tmp_path_factory):
    root = tmp_path_factory.mktemp("run")
    out = run.validate(
        panel,
        root=root,
        identity=None,
        years=(2018,),
        seal_boundary=date(2019, 1, 1),
        frozen_year=2019,
        grid=GRID,
        seeds=SEEDS,
    )
    return out, root


def test_both_hypotheses_get_a_verdict_and_every_policy_a_bracket(result) -> None:
    out, _ = result
    for hyp in (HYP_ADMIT, HYP_ENTRY):
        section = out[hyp]
        assert section["verdict"]["verdict"] in {"CONTINUE_TO_U8", "STOP"}
        assert section["rows"]["excluded_no_available_at"] == 0
        for runs in section["policies"].values():
            assert runs[BRACKET_CONSERVATIVE]["status"] == "ok"
            assert runs["optimistic_touch"]["status"] == "ok"
            assert runs["volume_share"]["status"] == "ok"
            assert runs["empirical"]["status"] == "unavailable"


def test_cash_control_is_exactly_flat(result) -> None:
    out, _ = result
    for hyp in (HYP_ADMIT, HYP_ENTRY):
        row = out[hyp]["policies"][pl.POL_CASH][BRACKET_CONSERVATIVE]
        assert row["net_vs_cash_total"] == 0
        assert row["entries"] == 0


def test_search_stays_inside_the_declared_budget(result) -> None:
    out, _ = result
    for hyp in (HYP_ADMIT, HYP_ENTRY):
        for fold in out[hyp]["folds"]:
            assert fold["configs_tried"] == len(GRID)
            assert len(fold["finalists"]) <= 3
            assert all(len(seeds) == len(SEEDS) for seeds in fold["fitted_sha256"])


def test_frozen_models_are_written_with_a_hash(result) -> None:
    out, root = result
    assert (root / "frozen_models.pkl").is_file()
    with (root / "frozen_models.pkl").open("rb") as handle:
        frozen = pickle.load(handle)
    assert set(frozen) == {"admit_quantile", "touch", "xret"}
    assert len(out["frozen_bundle_sha256"]) == 64
    assert json.loads((root / "validate_results.json").read_text())["frozen"]


def test_same_inputs_give_the_same_models(panel, tmp_path: Path, result) -> None:
    first, _ = result
    again = run.validate(
        panel,
        root=tmp_path,
        identity=None,
        years=(2018,),
        seal_boundary=date(2019, 1, 1),
        frozen_year=2019,
        grid=GRID,
        seeds=SEEDS,
    )
    assert again["frozen"] == first["frozen"]
    assert again[HYP_ADMIT]["policies"] == first[HYP_ADMIT]["policies"]


def test_year_masks_keep_purge_and_gap_for_training_rows(panel) -> None:
    t = np.arange(len(panel.calendar))
    masks = pl._masks(panel.calendar, t, 2018, purge=5, gap=5)
    boundary = next(i for i, d in enumerate(panel.calendar) if d >= date(2018, 1, 1))
    assert t[masks["train"]].max() == boundary - 11
    inner_boundary = next(
        i for i, d in enumerate(panel.calendar) if d >= date(2017, 1, 1)
    )
    assert t[masks["fit"]].max() == inner_boundary - 11
    assert t[masks["inner"]].min() == inner_boundary
    assert t[masks["inner"]].max() == boundary - 11
