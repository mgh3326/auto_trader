"""Read-only access to the immutable kr-corpus-v1 main-scope snapshot.

The sealed holdout (2025-01-01..2026-07-31) lives under a separate path and is
never opened here: every path is checked, and every session read must fall in
the main scope.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ARTIFACT_ROOT = Path("/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1")
RUN_ROOT = ARTIFACT_ROOT / "runs" / "kr-corpus-v1-20260803-1001"
CLAMP_ROOT = ARTIFACT_ROOT / "derived-views" / "clamp-admit-v1"
MAIN_FIRST = "2015-01-01"
MAIN_LAST = "2024-12-31"


class HoldoutAccessError(RuntimeError):
    """A read would touch the sealed holdout scope."""


def _guard(path: Path) -> Path:
    if "holdout" in path.parts:
        raise HoldoutAccessError(f"refusing to read sealed holdout path {path}")
    return path


def _read_all(files: list[Path], columns: list[str]) -> pa.Table:
    if not files:
        raise FileNotFoundError("no corpus files matched")
    tables = [pq.read_table(_guard(f), columns=columns) for f in files]
    table = pa.concat_tables(tables)
    sessions = table.column("session")
    if (
        pc.any(pc.less(sessions, MAIN_FIRST)).as_py()
        or pc.any(pc.greater(sessions, MAIN_LAST)).as_py()
    ):
        raise HoldoutAccessError("corpus rows outside the main scope were read")
    return table


@dataclass(frozen=True)
class CorpusPaths:
    run_root: Path = RUN_ROOT
    clamp_root: Path = CLAMP_ROOT

    def membership(self) -> pa.Table:
        files = sorted(
            self.run_root.glob("membership/market=*/year=*/session=*.parquet")
        )
        return _read_all(files, ["session", "market", "ticker"])

    def bars(self) -> pa.Table:
        files = sorted(self.run_root.glob("dataset/market=*/year=*/ticker=*.parquet"))
        return _read_all(
            files,
            ["session", "market", "ticker", "open", "high", "low", "close", "volume"],
        )

    def no_trade(self) -> pa.Table:
        files = sorted(self.clamp_root.glob("no_trade/market=*/year=*/*.parquet"))
        return _read_all(files, ["session", "market", "ticker", "close"])

    def manifest_sha256(self) -> str:
        return hashlib.sha256(
            (self.run_root / "manifest.json").read_bytes()
        ).hexdigest()

    def clamp_manifest_sha256(self) -> str:
        return hashlib.sha256(
            (self.clamp_root / "manifest.json").read_bytes()
        ).hexdigest()
