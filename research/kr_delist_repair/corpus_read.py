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


MAIN_YEARS = frozenset(f"year={y}" for y in range(2015, 2025))


def _guard(path: Path, root: Path) -> Path:
    """Refuse holdout paths, symlink escapes and non-main year partitions."""
    real = path.resolve()
    if "holdout" in path.parts or "holdout" in real.parts:
        raise HoldoutAccessError(f"refusing to read sealed holdout path {path}")
    if not real.is_relative_to(root.resolve()):
        raise HoldoutAccessError(f"{path} resolves outside {root}")
    if not any(part in MAIN_YEARS for part in path.parts):
        raise HoldoutAccessError(f"{path} is not in a main-scope year partition")
    return real


def _read_all(files: list[Path], columns: list[str], root: Path) -> pa.Table:
    if not files:
        raise FileNotFoundError("no corpus files matched")
    tables = [pq.read_table(_guard(f, root), columns=columns) for f in files]
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
        return _read_all(files, ["session", "market", "ticker"], self.run_root)

    def bars(self) -> pa.Table:
        files = sorted(self.run_root.glob("dataset/market=*/year=*/ticker=*.parquet"))
        return _read_all(
            files,
            ["session", "market", "ticker", "open", "high", "low", "close", "volume"],
            self.run_root,
        )

    def no_trade(self) -> pa.Table:
        files = sorted(self.clamp_root.glob("no_trade/market=*/year=*/*.parquet"))
        return _read_all(
            files, ["session", "market", "ticker", "close"], self.clamp_root
        )

    def manifest_sha256(self) -> str:
        return hashlib.sha256(
            (self.run_root / "manifest.json").read_bytes()
        ).hexdigest()

    def clamp_manifest_sha256(self) -> str:
        return hashlib.sha256(
            (self.clamp_root / "manifest.json").read_bytes()
        ).hexdigest()
