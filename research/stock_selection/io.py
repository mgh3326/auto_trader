"""Local hash-verified CSV inputs and JSON outputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

DEFAULT_ROOT = Path(
    os.environ.get("STOCK_SELECTION_CACHE", "research/stock_selection/.cache")
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON never contains non-standard NaN/Infinity values.
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return path


def read_bars(root: Path, market: str) -> tuple[pd.DataFrame, dict]:
    path = (root / market).resolve() / "bars.csv"
    meta = json.loads(path.with_suffix(".json").read_text())
    if meta["market"] != market or digest(path) != meta["sha256"]:
        raise ValueError("input market/hash mismatch")
    frame = pd.read_csv(path, dtype={"symbol": str}, float_precision="round_trip")
    return frame, meta


def add_input_args(parser) -> None:
    parser.add_argument("--market", choices=("kr", "us", "crypto"), required=True)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--liquid-n", type=int, default=None)
