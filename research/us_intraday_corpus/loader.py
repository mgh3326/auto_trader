"""The only sanctioned reader for this corpus -- and it enforces the label.

§0 pitfall 2 (the reason this module exists)
--------------------------------------------
A consumer of `us-corpus-v1` could point pandas at `dataset/` and compute a
backtest without ever encountering the survivorship warning. This loader makes
that impossible through the sanctioned path: you cannot obtain a dataframe
without passing `acknowledge_survivorship_bias=True`, and you cannot obtain
holdout data at all.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from . import access_log, config, labels


class SurvivorshipBiasNotAcknowledged(RuntimeError):
    """Raised when a caller tries to read the corpus without the label."""


class HoldoutAccessDenied(RuntimeError):
    """Raised on any attempt to read the sealed holdout partition."""


def load_dataset(
    *,
    acknowledge_survivorship_bias: bool = False,
    frequency: str = "1h",
    columns: list[str] | None = None,
) -> Any:
    """Load the exploration partition as a pyarrow Table.

    Args:
        acknowledge_survivorship_bias: must be True. This is not ceremony --
            it is the contract that the daily sister corpus lacked.
        frequency: "1h" or "1m".
        columns: optional column projection.

    Raises:
        SurvivorshipBiasNotAcknowledged: when the flag is not set.
    """
    if not acknowledge_survivorship_bias:
        raise SurvivorshipBiasNotAcknowledged(
            "Refusing to hand over "
            f"{config.CORPUS_ID}. {labels.SURVIVORSHIP_NOTE} "
            "Pass acknowledge_survivorship_bias=True once you have read this."
        )

    import pyarrow.dataset as ds

    root = config.DATASET_DIR / f"freq={frequency}"
    if not root.exists():
        raise FileNotFoundError(f"no exploration data at {root}")
    return ds.dataset(str(root), format="parquet", partitioning="hive").to_table(
        columns=columns
    )


def load_holdout(*_args, **_kwargs) -> Any:
    """Always raises. The holdout is write-only until forward-OOS evaluation."""
    raise HoldoutAccessDenied(
        f"{config.HOLDOUT_DIR} is sealed (window "
        f"{config.HOLDOUT[0]}..{config.HOLDOUT[1]}). It is written once and never "
        "read by this codebase. Unsealing is an orch-level decision, not a code path."
    )


def assert_not_holdout_path(path: Path) -> None:
    """Guard for ad-hoc scripts that build their own paths.

    Symlink- and case-resolved: `HOLDOUT/…` and `link -> holdout/…` both raise.
    """
    if access_log.is_holdout_path(path):
        raise HoldoutAccessDenied(
            f"{path} is inside a sealed holdout directory "
            f"(canonical: {access_log.canonical(path)})"
        )


def assert_not_holdout_date(day: _dt.date) -> None:
    """Guard on the date axis as well as the path axis."""
    if config.is_holdout_date(day):
        raise HoldoutAccessDenied(
            f"{day} falls inside the sealed holdout window "
            f"{config.HOLDOUT[0]}..{config.HOLDOUT[1]}"
        )
