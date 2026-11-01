"""Causal daily dataset: one row per (symbol, membership session).

Columns
-------
symbol, market, session (date32), session_state ∈ {valid, no_trade, invalid_ohlc},
open/high/low/volume (valid rows only), close (valid rows; no_trade rows carry
the source reference close), available_at (UTC timestamp; session 16:00 KST),
is_terminal_session, terminal_outcome, payout_semantics, delist_date,
terminal_available_at.

Terminal fields are set on exactly one row per symbol whose membership ended
inside the main scope: its last membership session.  They are never
forward-filled, and no row exists on or after a symbol's delist date.  A
symbol whose membership ended without a resolved terminal event carries
``terminal_outcome="unresolved"`` on that row instead of being dropped.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from research.kr_delist_repair.model import ExportConflict
from research.kr_delist_repair.resolve import Resolution

UNRESOLVED = "unresolved"
_UTC = "UTC"
_BAR_PUBLISH_UTC_OFFSET_HOURS = 7  # 16:00 Asia/Seoul == 07:00 UTC (no DST in Korea)


def _to_date(col: pa.ChunkedArray) -> pa.ChunkedArray:
    return pc.cast(pc.strptime(col, format="%Y-%m-%d", unit="s"), pa.date32())


def build_panel(
    membership: pa.Table,
    bars: pa.Table,
    no_trade: pa.Table,
    resolutions: Iterable[Resolution],
) -> pa.Table:
    keys = ["session", "market", "ticker"]
    bars_t = bars.select(
        [*keys, "open", "high", "low", "close", "volume"]
    ).append_column("_valid", pa.array([True] * bars.num_rows))
    nt = (
        no_trade.select(keys)
        .append_column("ref_close", no_trade.column("close"))
        .append_column("_no_trade", pa.array([True] * no_trade.num_rows))
    )
    panel = membership.select(keys).join(bars_t, keys, join_type="left outer")
    panel = panel.join(nt, keys, join_type="left outer")
    if panel.num_rows != membership.num_rows:
        raise ExportConflict("bar or no-trade rows duplicate a membership row")

    valid = pc.fill_null(panel.column("_valid"), False)
    no_tr = pc.fill_null(panel.column("_no_trade"), False)
    if pc.any(pc.and_(valid, no_tr)).as_py():
        raise ExportConflict("a session is both a valid bar and a no-trade day")
    state = pc.if_else(valid, "valid", pc.if_else(no_tr, "no_trade", "invalid_ohlc"))
    close = pc.if_else(valid, panel.column("close"), panel.column("ref_close"))

    session = _to_date(panel.column("session"))
    available = pc.add(
        pc.cast(pc.cast(session, pa.timestamp("s")), pa.timestamp("us")),
        pa.scalar(_BAR_PUBLISH_UTC_OFFSET_HOURS * 3_600_000_000, pa.duration("us")),
    )
    available = pc.cast(available, pa.timestamp("us", tz=_UTC))

    base = pa.table(
        {
            "symbol": panel.column("ticker"),
            "market": panel.column("market"),
            "session": session,
            "session_state": state,
            "open": panel.column("open"),
            "high": panel.column("high"),
            "low": panel.column("low"),
            "close": close,
            "volume": panel.column("volume"),
            "available_at": available,
        }
    )
    dup = base.group_by(["symbol", "session"]).aggregate([("market", "count")])
    if pc.any(pc.greater(dup.column("market_count"), 1)).as_py():
        raise ExportConflict("a symbol has two membership rows on one session")

    last = base.group_by("symbol").aggregate([("session", "max")])
    main_end = pc.max(base.column("session")).as_py()
    last_by_symbol = dict(
        zip(
            last.column("symbol").to_pylist(),
            last.column("session_max").to_pylist(),
            strict=True,
        )
    )
    by_symbol = {r.symbol: r for r in resolutions}

    t_sym: list[str] = []
    t_session: list[date] = []
    t_outcome: list[str] = []
    t_payout: list[str | None] = []
    t_delist: list[date | None] = []
    t_avail: list[datetime | None] = []
    for symbol, last_session in sorted(last_by_symbol.items()):
        if last_session >= main_end:
            continue
        res = by_symbol.get(symbol)
        t_sym.append(symbol)
        t_session.append(last_session)
        if res is not None and res.resolved:
            delist = date.fromisoformat(res.delist_date)
            if delist <= last_session:
                raise ExportConflict(
                    f"{symbol}: corpus has membership on {last_session} but delist date is {delist}"
                )
            t_outcome.append(res.terminal_outcome)
            t_payout.append(res.payout_semantics)
            t_delist.append(delist)
            t_avail.append(datetime.fromisoformat(res.information_available_at))
        else:
            t_outcome.append(UNRESOLVED)
            t_payout.append(None)
            t_delist.append(None)
            t_avail.append(None)

    terminal = pa.table(
        {
            "symbol": pa.array(t_sym, pa.string()),
            "session": pa.array(t_session, pa.date32()),
            "is_terminal_session": pa.array([True] * len(t_sym)),
            "terminal_outcome": pa.array(t_outcome, pa.string()),
            "payout_semantics": pa.array(t_payout, pa.string()),
            "delist_date": pa.array(t_delist, pa.date32()),
            "terminal_available_at": pa.array(t_avail, pa.timestamp("us", tz=_UTC)),
        }
    )
    out = base.join(terminal, ["symbol", "session"], join_type="left outer")
    out = out.set_column(
        out.schema.get_field_index("is_terminal_session"),
        "is_terminal_session",
        pc.fill_null(out.column("is_terminal_session"), False),
    )
    out = out.sort_by([("symbol", "ascending"), ("session", "ascending")])
    validate_panel(out, resolutions)
    return out


def validate_panel(
    panel: pa.Table, resolutions: Iterable[Resolution]
) -> dict[str, int]:
    """Fail closed on any causal or terminal-handling violation."""
    if panel.column("available_at").null_count:
        raise ExportConflict("a row has no available_at")
    delist = {
        r.symbol: date.fromisoformat(r.delist_date)
        for r in resolutions
        if r.resolved and r.delist_date
    }
    symbols = panel.column("symbol").to_pylist()
    sessions = panel.column("session").to_pylist()
    terminal = panel.column("is_terminal_session").to_pylist()
    outcome = panel.column("terminal_outcome").to_pylist()
    t_avail = panel.column("terminal_available_at").to_pylist()

    last: dict[str, date] = {}
    seen: set[tuple[str, date]] = set()
    terminal_rows: dict[str, date] = {}
    after_delist = 0
    for sym, ses, is_t, out, avail in zip(
        symbols, sessions, terminal, outcome, t_avail, strict=True
    ):
        if (sym, ses) in seen:
            raise ExportConflict(f"duplicate row {sym} {ses}")
        seen.add((sym, ses))
        last[sym] = max(last.get(sym, ses), ses)
        cut = delist.get(sym)
        if cut is not None and ses >= cut:
            after_delist += 1
            raise ExportConflict(
                f"{sym} appears on {ses}, on or after its delist date {cut}"
            )
        if is_t:
            if sym in terminal_rows:
                raise ExportConflict(f"{sym} has more than one terminal row")
            terminal_rows[sym] = ses
            if out is None:
                raise ExportConflict(f"{sym} terminal row has no terminal_outcome")
            if out != UNRESOLVED and avail is None:
                raise ExportConflict(f"{sym} terminal row has no terminal_available_at")
        elif out is not None:
            raise ExportConflict(
                f"{sym} carries terminal_outcome on a non-terminal row {ses}"
            )
    for sym, ses in terminal_rows.items():
        if last[sym] != ses:
            raise ExportConflict(
                f"{sym} terminal row {ses} is not its last row {last[sym]}"
            )
    return {
        "rows": panel.num_rows,
        "symbols": len(last),
        "terminal_rows": len(terminal_rows),
        "rows_on_or_after_delist_date": after_delist,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_panel(panel: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(panel, path, compression="zstd")


def write_manifest(
    path: Path, files: dict[str, Path], extra: dict[str, object]
) -> dict[str, object]:
    entries = {}
    for label, file in sorted(files.items()):
        entries[label] = {
            "path": str(file),
            "bytes": file.stat().st_size,
            "sha256": sha256_file(file),
        }
    manifest = {"files": entries, **extra}
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return manifest
