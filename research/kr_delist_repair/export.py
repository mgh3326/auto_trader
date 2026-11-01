"""Causal daily dataset: one row per (symbol, membership session).

Columns
-------
symbol, listing_id (``symbol#segment``; a reused code is a new listing), market, session (date32), session_state ∈ {valid, no_trade, invalid_ohlc},
open/high/low/volume (valid rows only), close (valid rows; no_trade rows carry
the source reference close), available_at (UTC timestamp; session 16:00 KST),
is_terminal_session, terminal_outcome, payout_semantics, delist_date,
terminal_available_at.

Terminal fields are set on exactly one row per listing whose membership ended
inside the main scope: its last membership session.  They are never
forward-filled, and no row exists on or after a symbol's delist date.  A
symbol whose membership ended without a resolved terminal event carries
``terminal_outcome="unresolved"`` on that row instead of being dropped.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from research.kr_delist_repair.model import ExportConflict
from research.kr_delist_repair.resolve import Presence, Resolution

UNRESOLVED = "unresolved"
_UTC = "UTC"
_BAR_PUBLISH_UTC_OFFSET_HOURS = 7  # 16:00 Asia/Seoul == 07:00 UTC (no DST in Korea)


def _to_date(col: pa.ChunkedArray) -> pa.ChunkedArray:
    return pc.cast(pc.strptime(col, format="%Y-%m-%d", unit="s"), pa.date32())


def _bar_available_at(session: pa.ChunkedArray) -> pa.ChunkedArray:
    available = pc.add(
        pc.cast(pc.cast(session, pa.timestamp("s")), pa.timestamp("us")),
        pa.scalar(_BAR_PUBLISH_UTC_OFFSET_HOURS * 3_600_000_000, pa.duration("us")),
    )
    return pc.cast(available, pa.timestamp("us", tz=_UTC))


def _listing_ids(base: pa.Table, presences: Mapping[str, Presence]) -> pa.ChunkedArray:
    symbol = base.column("symbol")
    ids = pc.binary_join_element_wise(symbol, pa.scalar("#0"), "")
    by_symbol: dict[str, list[Presence]] = {}
    for p in presences.values():
        by_symbol.setdefault(p.symbol, []).append(p)
    for sym, segs in by_symbol.items():
        if len(segs) == 1 and segs[0].segment == 0:
            continue
        for p in segs:
            mask = pc.and_(
                pc.equal(symbol, sym),
                pc.and_(
                    pc.greater_equal(
                        base.column("session"), pa.scalar(p.first_session)
                    ),
                    pc.less_equal(base.column("session"), pa.scalar(p.last_session)),
                ),
            )
            ids = pc.if_else(mask, pa.scalar(p.listing_id), ids)
    return ids


def build_panel(
    membership: pa.Table,
    bars: pa.Table,
    no_trade: pa.Table,
    resolutions: Iterable[Resolution],
    presences: Mapping[str, Presence],
) -> pa.Table:
    resolutions = list(resolutions)
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
            "available_at": _bar_available_at(session),
        }
    )
    dup = base.group_by(["symbol", "session"]).aggregate([("market", "count")])
    if pc.any(pc.greater(dup.column("market_count"), 1)).as_py():
        raise ExportConflict("a symbol has two membership rows on one session")
    base = base.add_column(1, "listing_id", _listing_ids(base, presences))

    main_end = pc.max(base.column("session")).as_py()
    by_listing = {r.listing_id: r for r in resolutions}

    t_sym: list[str] = []
    t_session: list[date] = []
    t_outcome: list[str] = []
    t_payout: list[str | None] = []
    t_delist: list[date | None] = []
    t_avail: list[datetime | None] = []
    for lid, p in sorted(presences.items()):
        if p.last_session >= main_end:
            continue
        res = by_listing.get(lid)
        t_sym.append(p.symbol)
        t_session.append(p.last_session)
        if res is not None and res.resolved:
            delist = date.fromisoformat(res.delist_date)
            if delist <= p.last_session:
                raise ExportConflict(
                    f"{lid}: corpus has membership on {p.last_session} "
                    f"but delist date is {delist}"
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


_TERMINAL_VALUE_COLUMNS = (
    "terminal_outcome",
    "payout_semantics",
    "delist_date",
    "terminal_available_at",
)


def validate_panel(
    panel: pa.Table, resolutions: Iterable[Resolution]
) -> dict[str, int]:
    """Fail closed on any causal or terminal-handling violation.

    Checks: ``available_at`` is exactly session 16:00 KST on every row; one row
    per (symbol, session); a listing's sessions are contiguous in the panel
    calendar (a gap must be a separate listing); every listing that ends before
    the last session has exactly one terminal row, on its last session; terminal
    values appear on no other row; no row on or after a resolved delist date.
    """
    if panel.column("available_at").null_count:
        raise ExportConflict("a row has no available_at")
    expected = _bar_available_at(panel.column("session"))
    if not pc.all(pc.equal(panel.column("available_at"), expected)).as_py():
        raise ExportConflict(
            "available_at is not session 16:00 Asia/Seoul on every row"
        )

    delist = {
        r.listing_id: date.fromisoformat(r.delist_date)
        for r in resolutions
        if r.resolved and r.delist_date
    }
    calendar = sorted(pc.unique(panel.column("session")).to_pylist())
    index = {d: i for i, d in enumerate(calendar)}
    main_end = calendar[-1]
    cols = {
        c: panel.column(c).to_pylist()
        for c in ("symbol", "listing_id", "session", "is_terminal_session")
    }
    values = [panel.column(c).to_pylist() for c in _TERMINAL_VALUE_COLUMNS]

    seen: set[tuple[str, date]] = set()
    sessions_of: dict[str, list[date]] = {}
    terminal_rows: dict[str, list[date]] = {}
    violations: list[str] = []
    after_delist = 0
    for i, (sym, lid, ses, is_t) in enumerate(
        zip(*(cols[c] for c in cols), strict=True)
    ):
        if (sym, ses) in seen:
            raise ExportConflict(f"duplicate row {sym} {ses}")
        seen.add((sym, ses))
        sessions_of.setdefault(lid, []).append(ses)
        cut = delist.get(lid)
        if cut is not None and ses >= cut:
            after_delist += 1
            violations.append(
                f"{lid} appears on {ses}, on or after its delist date {cut}"
            )
        row_values = [v[i] for v in values]
        if is_t:
            terminal_rows.setdefault(lid, []).append(ses)
            if row_values[0] is None:
                violations.append(f"{lid} terminal row has no terminal_outcome")
            elif row_values[0] != UNRESOLVED and row_values[3] is None:
                violations.append(f"{lid} terminal row has no terminal_available_at")
        elif any(v is not None for v in row_values):
            violations.append(
                f"{lid} carries terminal values on a non-terminal row {ses}"
            )

    for lid, ses_list in sessions_of.items():
        ses_list.sort()
        idx = [index[d] for d in ses_list]
        if idx != list(range(idx[0], idx[0] + len(idx))):
            violations.append(f"{lid} has a gap inside one listing")
        last = ses_list[-1]
        rows = terminal_rows.get(lid, [])
        if last < main_end and rows != [last]:
            violations.append(f"{lid} ends on {last} but has terminal rows {rows}")
        if last >= main_end and rows:
            violations.append(
                f"{lid} is alive at scope end but has terminal rows {rows}"
            )
    if violations:
        raise ExportConflict(
            f"{len(violations)} violation(s), {after_delist} row(s) on or after a "
            f"delist date; first: {violations[0]}"
        )
    return {
        "rows": panel.num_rows,
        "listings": len(sessions_of),
        "terminal_rows": sum(len(v) for v in terminal_rows.values()),
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
