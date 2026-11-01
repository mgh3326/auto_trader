"""Frozen literals of the run. Changing one is a new manifest version."""

from __future__ import annotations

import itertools
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from research.eval_contract.contract import sha256_canonical

TEST_START = date(2023, 1, 1)
TEST_END = date(2024, 12, 31)
VALIDATION_YEARS: tuple[int, ...] = (2019, 2020, 2021, 2022)
TRAIN_START = date(2015, 1, 1)

HYP_ADMIT = "H-admit"
HYP_ENTRY = "H-entry-price"

ADMIT_HORIZON = 5  # t+1 open -> t+5 close
TOUCH_WINDOW = 5
XRET_HOLD = 20
ENTRY_HORIZON = TOUCH_WINDOW + XRET_HOLD  # 25 sessions after t at most
MIN_GAP = 5


@dataclass(frozen=True, slots=True)
class HypothesisSplit:
    """Purge and gap in sessions, as the registered manifest states them."""

    purge: int
    gap: int


SPLITS: dict[str, HypothesisSplit] = {
    HYP_ADMIT: HypothesisSplit(purge=ADMIT_HORIZON, gap=max(MIN_GAP, ADMIT_HORIZON)),
    HYP_ENTRY: HypothesisSplit(purge=ENTRY_HORIZON, gap=max(MIN_GAP, ENTRY_HORIZON)),
}

# Portfolio frame, shared by every policy (D3 constants).
CAPITAL = Decimal("10000000")
ORDER_NOTIONAL = Decimal("300000")
MAX_NEW_PER_SESSION = 3
FEE_RATE = Decimal("0.00215")
SENSITIVITY_FEE_RATE = Decimal("0.00415")
ROUND_TRIP_COST = float(FEE_RATE * 2)
PARTICIPATION = Decimal("0.1")

# Order templates.
ADMIT_ENTRY_CAP = Decimal("1.02")  # DAY limit at prior close +2%: fills at the open
NEAR_LEVEL = Decimal("0.995")
MID_LEVEL = Decimal("0.985")
L1_LEVEL = Decimal("0.97")  # D3 rung L1
SELL_FLOOR = Decimal("0.70")  # marketable closing-auction sell (KRX -30% limit)

# Admit rule on predicted quantiles, fixed before the first fit.
ADMIT_MIN_Q10 = -0.10

# Frozen controls.
LIQUIDITY_FLOOR_KRW = 100_000_000.0  # median 20-session traded value
MOMENTUM_LOOKBACK = 60
MEAN_REVERSION_LOOKBACK = 5

# Model search.
MAX_CONFIGS_PER_FAMILY = 12  # the brief allows at most 20
FINALISTS = 3
SEEDS: tuple[int, ...] = (1441, 1442, 1443)
QUANTILES: tuple[float, ...] = (0.1, 0.5, 0.9)
GRID_SEED = 1441


def _grid() -> tuple[dict[str, Any], ...]:
    space = list(
        itertools.product(
            (15, 31, 63),  # num_leaves
            (50, 200, 500),  # min_data_in_leaf
            (0.03, 0.08),  # learning_rate
            (150, 400),  # n_estimators
            (0.5, 0.8),  # feature_fraction
            (0.0, 10.0),  # lambda_l2
        )
    )
    picks = random.Random(GRID_SEED).sample(space, MAX_CONFIGS_PER_FAMILY)
    return tuple(
        {
            "num_leaves": p[0],
            "min_data_in_leaf": p[1],
            "learning_rate": p[2],
            "n_estimators": p[3],
            "feature_fraction": p[4],
            "lambda_l2": p[5],
            "bagging_fraction": 0.7,
            "bagging_freq": 1,
            "max_bin": 63,
        }
        for p in picks
    )


CONFIG_GRID: tuple[dict[str, Any], ...] = _grid()


def grid_sha256(grid: Sequence[Mapping[str, Any]] = CONFIG_GRID) -> str:
    canon = [{k: str(v) for k, v in cfg.items()} for cfg in grid]
    return sha256_canonical(canon)


# Gate, fixed before the first fit. Evaluated on the 2019-2022 walk-forward path
# under the conservative fill end only (see ``gate``).
GATE_ALPHA = 0.10
GATE_DELTA_MIN_ANNUAL = 0.02
GATE_MIN_POSITIVE_YEARS = 3
BOOTSTRAP_BLOCK = 20
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 1441
SESSIONS_PER_YEAR = 245
