"""Version ledger: v1 code, every input file, the price cache, the sample period."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from research.rob1383_buy_drought.prices import KST

V1_PACKAGE = "research/rob1383_buy_drought"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mtime(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, UTC)
        .astimezone(KST)
        .isoformat(timespec="minutes")
    )


def _rows(path: Path) -> int | str:
    if path.suffix == ".csv":
        return len(pd.read_csv(path, dtype=str))
    if path.suffix == ".json":
        payload = json.loads(path.read_text())
        for key in ("entries", "result"):
            v = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(v, list):
                return len(v)
            if isinstance(v, dict) and isinstance(v.get("entries"), list):
                return len(v["entries"])
        return "object"
    return "n/a"


def _collected_at(path: Path) -> str | None:
    if path.suffix != ".json":
        return None
    payload = json.loads(path.read_text())
    if isinstance(payload, dict):
        return payload.get("_fetched_at_utc") or payload.get("fetched_at")
    return None


def input_table(inputs: Path) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "file": p.name,
                "sha256": _sha256(p),
                "rows": _rows(p),
                "collected_at_field": _collected_at(p),
                "file_mtime_kst": _mtime(p),
            }
            for p in sorted(inputs.iterdir())
            if p.is_file()
        ]
    )


def cache_table(cache_dir: Path, symbols: list[str]) -> pd.DataFrame:
    """Daily/hourly coverage of the KR symbols and indices actually used."""

    rows = []
    for sym in symbols:
        safe = sym.replace("^", "IDX_")
        for kind, path in (
            ("daily", cache_dir / f"kr__{safe}.parquet"),
            ("hourly", cache_dir / "hourly" / f"kr__{safe}.parquet"),
        ):
            rec = {"symbol": sym, "kind": kind, "exists": path.exists()}
            if path.exists():
                f = pd.read_parquet(path, columns=["session_date"])
                d = pd.to_datetime(f["session_date"])
                rec.update(
                    rows=len(f),
                    first=str(d.min().date()),
                    last=str(d.max().date()),
                    file_mtime_kst=_mtime(path),
                )
                meta = path.with_suffix(".json")
                if kind == "daily" and meta.exists():
                    rec["fetched_at"] = json.loads(meta.read_text()).get("fetched_at")
            rows.append(rec)
    return pd.DataFrame(rows)


def cache_summary(cache_dir: Path) -> dict[str, object]:
    files = [p for p in cache_dir.rglob("*") if p.is_file()]
    return {
        "cache_files": len(files),
        "cache_parquet": sum(p.suffix == ".parquet" for p in files),
        "cache_latest_mtime_kst": max(_mtime(p) for p in files) if files else None,
    }


def git_info(repo: Path) -> dict[str, str | bool]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "head": run("rev-parse", "HEAD"),
        "v1_last_commit": run("log", "-1", "--format=%H %s", "--", V1_PACKAGE),
        "v1_tree": run("rev-parse", f"HEAD:{V1_PACKAGE}"),
        # tracked + untracked changes outside ignored paths (results/, .cache/)
        "worktree_clean": run("status", "--porcelain") == "",
    }
