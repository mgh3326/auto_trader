"""U2 causal export as dense listing x session arrays.

Loading is hash-pinned and fail-closed: the parquet must match the export
manifest, and sessions on or after ``TEST_START`` cannot be read without
``allow_sealed=True``, which only the post-freeze diagnostic stage passes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from research.kr_boosting.config import TEST_START

KST = ZoneInfo("Asia/Seoul")
PARQUET_NAME = "kr_daily_causal.v1.parquet"
MANIFEST_NAME = "manifest.json"

STATE_ABSENT, STATE_VALID, STATE_NO_TRADE, STATE_INVALID = 0, 1, 2, 3
_STATE = {
    "valid": STATE_VALID,
    "no_trade": STATE_NO_TRADE,
    "invalid_ohlc": STATE_INVALID,
}

PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
TERMINAL_COLUMNS = (
    "is_terminal_session",
    "terminal_outcome",
    "payout_semantics",
    "terminal_available_at",
)
_READ_COLUMNS = (
    "symbol",
    "listing_id",
    "market",
    "session",
    "session_state",
    *PRICE_COLUMNS,
    "available_at",
    *TERMINAL_COLUMNS,
)
NAT = np.iinfo(np.int64).min


class SealedPeriodRead(PermissionError):
    """A read would touch the sealed period before the model is frozen."""


class ExportIdentityError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ExportIdentity:
    parquet_sha256: str
    manifest_sha256: str
    rows: int


def verify_export(export_dir: Path) -> ExportIdentity:
    manifest_path = export_dir / MANIFEST_NAME
    parquet_path = export_dir / PARQUET_NAME
    entry = json.loads(manifest_path.read_text())["files"]["kr_daily_causal"]
    actual = sha256_file(parquet_path)
    if actual != entry["sha256"]:
        raise ExportIdentityError(
            f"{PARQUET_NAME} sha256 {actual} != manifest {entry['sha256']}"
        )
    return ExportIdentity(actual, sha256_file(manifest_path), int(entry["rows"]))


def read_rows(
    parquet_path: Path, *, last_session: date, allow_sealed: bool = False
) -> pd.DataFrame:
    if last_session >= TEST_START and not allow_sealed:
        raise SealedPeriodRead(
            f"reading through {last_session} touches the sealed period"
        )
    table = pq.read_table(
        parquet_path,
        columns=list(_READ_COLUMNS),
        filters=[("session", "<=", last_session)],
    )
    return table.to_pandas()


@dataclass(slots=True)
class Panel:
    calendar: list[date]
    listing_ids: list[str]
    symbols: list[str]
    is_kosdaq: np.ndarray
    state: np.ndarray  # (L, S) int8
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    available_at: np.ndarray  # (L, S) int64 microseconds UTC, NAT when unknown
    last_idx: np.ndarray  # last membership session of each listing
    terminal_idx: np.ndarray  # terminal session index or -1
    terminal_available_at: np.ndarray  # int64 microseconds UTC, NAT when none
    payout: list[str | None]

    @property
    def shape(self) -> tuple[int, int]:
        return self.state.shape


def _to_us(series: pd.Series) -> np.ndarray:
    values = pd.to_datetime(series, utc=True).astype("datetime64[us, UTC]")
    out = values.dt.tz_localize(None).astype("datetime64[us]").astype("int64")
    return np.where(series.isna().to_numpy(), NAT, out.to_numpy())


def build_panel(rows: pd.DataFrame) -> Panel:
    if rows.duplicated(["listing_id", "session"]).any():
        raise ValueError("duplicate (listing_id, session) row")
    calendar = sorted(set(rows["session"]))
    cal_pos = {d: i for i, d in enumerate(calendar)}
    listing_ids = sorted(rows["listing_id"].unique())
    pos = {x: i for i, x in enumerate(listing_ids)}
    n_l, n_s = len(listing_ids), len(calendar)

    li = rows["listing_id"].map(pos).to_numpy()
    s = rows["session"].map(cal_pos).to_numpy()

    def dense(col: str) -> np.ndarray:
        arr = np.full((n_l, n_s), np.nan, dtype=np.float64)
        arr[li, s] = rows[col].to_numpy(dtype=np.float64)
        return arr

    state = np.zeros((n_l, n_s), dtype=np.int8)
    state[li, s] = rows["session_state"].map(_STATE).to_numpy(dtype=np.int8)
    available = np.full((n_l, n_s), NAT, dtype=np.int64)
    available[li, s] = _to_us(rows["available_at"])

    meta = rows.sort_values("session").groupby("listing_id", sort=True).last()
    symbols = meta["symbol"].tolist()
    is_kosdaq = (meta["market"] == "KOSDAQ").to_numpy()

    last_idx = np.full(n_l, -1, dtype=np.int64)
    np.maximum.at(last_idx, li, s)

    terminal_idx = np.full(n_l, -1, dtype=np.int64)
    terminal_at = np.full(n_l, NAT, dtype=np.int64)
    payout: list[str | None] = [None] * n_l
    term = rows[rows["is_terminal_session"].astype(bool)]
    if len(term):
        tl = term["listing_id"].map(pos).to_numpy()
        ts = term["session"].map(cal_pos).to_numpy()
        terminal_idx[tl] = ts
        terminal_at[tl] = _to_us(term["terminal_available_at"])
        for i, value in zip(tl, term["payout_semantics"], strict=True):
            payout[int(i)] = None if pd.isna(value) else str(value)

    return Panel(
        calendar=calendar,
        listing_ids=listing_ids,
        symbols=symbols,
        is_kosdaq=is_kosdaq,
        state=state,
        open=dense("open"),
        high=dense("high"),
        low=dense("low"),
        close=dense("close"),
        volume=dense("volume"),
        available_at=available,
        last_idx=last_idx,
        terminal_idx=terminal_idx,
        terminal_available_at=terminal_at,
        payout=payout,
    )


def kst_us(session: date, hour: int, minute: int) -> int:
    moment = datetime(
        session.year, session.month, session.day, hour, minute, tzinfo=KST
    )
    return int(moment.timestamp() * 1_000_000)
