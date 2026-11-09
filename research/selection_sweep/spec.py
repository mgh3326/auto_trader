"""Frozen grid, windows, costs and rules of the sweep.

Everything the registered manifest pins lives here. Changing a value is a new
manifest version, never an edit under an old hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

SWEEP_ID = "ROB-1474-sweep"
MANIFEST_VERSION = 2
MARKETS = ("us", "crypto")
HORIZONS = (1, 3, 5, 10, 20)
SIDES = ("long_only", "long_short")
RANKERS = (
    "momentum_20",
    "momentum_60",
    "reversal_5",
    "reversal_1",
    "low_volatility_20",
    "high_distance_52w",
    "volume_shock_20",
)
COST_BPS = {"us": 10.0, "crypto": 15.0}  # assumed per side, same as stock_selection
SIDES_PAID = {"long_only": 2, "long_short": 4}  # entry+exit per leg, no netting
HIGH_WINDOW = {"us": 252, "crypto": 364}
LIQUID_N = {"us": 100, "crypto": 60}
MIN_UNIVERSE = 30  # a decision date with fewer eligible names is skipped
DECILE = 0.10  # n = ceil(DECILE * N)
SEEDS = tuple(range(1, 201))
MIN_KNOWN_SHARE = 0.5  # of selected names, else the date is unknown

# Windows are decision dates; an outcome counts only if its exit date is inside.
EXPLORATION = ("2021-01-04", "2023-12-31")
VALIDATION = ("2024-02-01", "2024-12-31")
HISTORY_START = {"us": "2019-06-03", "crypto": "2019-06-01"}
YEARS = ("2021", "2022", "2023")

# Carry-forward rule, evaluated on exploration only. All must hold.
CARRY_MIN_T = 2.0
CARRY_MAX_RANDOM_P = 0.05
CARRY_MIN_POSITIVE_YEARS = 2
CARRY_CAP = 10  # at most this many cells, best exploration t first
ALPHA = 0.05

US_SAMPLE_SEED = 20261109
US_SAMPLE_SIZE = 900
US_LISTED_BY = "2019-12-31"
BENCHMARK = {"us": "SPY", "crypto": "BTCUSDT"}


@dataclass(frozen=True)
class Cell:
    market: str
    ranker: str
    horizon: int
    side: str

    @property
    def key(self) -> str:
        return f"{self.market}|{self.ranker}|h{self.horizon}|{self.side}"


def grid() -> list[Cell]:
    return [
        Cell(m, r, h, s) for m, r, h, s in product(MARKETS, RANKERS, HORIZONS, SIDES)
    ]
