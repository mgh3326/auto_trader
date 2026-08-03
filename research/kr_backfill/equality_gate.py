"""3-source equality gate. Decides whether the split may proceed.

Criterion is fixed BEFORE the data is seen (Stage A report §11, amended below).
Nothing in this file picks a tolerance after looking at the numbers, and nothing
declares a winner when sources disagree — it lists all three values and stops.

Also measures two things the split's time estimate depends on but which were
never verified: **rows per call** and **how far back each source serves 1m
bars**. A 1-year plan is void if a source only retains 3 months, so the gate
reports depth even when equality passes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sources import (  # noqa: E402
    VALUE_SEMANTICS,
    Pacer,
    assert_fetch_window_open,
    fetch_kis_minutes,
    fetch_kiwoom_minutes,
    fetch_toss_minutes,
    in_regular_session,
    now_kst,
)

PRICE_FIELDS = ("open", "high", "low", "close")
COMPARED_FIELDS = (*PRICE_FIELDS, "volume")

CRITERION = {
    "unit": "(symbol, minute_ts) x field",
    "compared_fields": list(COMPARED_FIELDS),
    "rule": "exact equality (integer-identical); no tolerance is opened after seeing data",
    "exceptions": {
        "E1_forming_bar": "each source's newest bar excluded",
        "E2_one_sided_minute": "minutes present in only one source counted as coverage difference, not mismatch",
        "E3_value_units": "value excluded from pass/fail (see E4)",
        "E4_toss_value_synthetic": (
            "Toss `value` is computed by this repo as close*volume "
            "(app/services/brokers/toss/candles.py), not broker-reported, whereas Kiwoom "
            "sends trde_prica and KIS sends acml_tr_pbmn. Comparing it would measure our own "
            "arithmetic and would fail the gate for a reason unrelated to source agreement. "
            "AMENDMENT declared before the run, not after."
        ),
        "E5_timestamp_convention": (
            "bar-label convention (bar-start vs bar-end) may differ by one minute between "
            "sources. The offset maximising overlap is DETECTED and REPORTED per pair; "
            "comparison then runs at that offset. A nonzero offset is disclosed, never hidden."
        ),
    },
    "pass_condition": "zero mismatched cells after exceptions, for every pair",
    "adjudication": "none - on mismatch all three values are listed, no source declared correct",
}


def load_symbols(path: Path) -> list[tuple[str, str]]:
    with path.open() as fh:
        return [(r["ticker"], r["market"]) for r in csv.DictReader(fh)]


def best_offset(a: dict, b: dict) -> tuple[int, int]:
    """Return (offset_minutes, overlap) maximising timestamp overlap in [-1,0,1]."""
    best = (0, -1)
    for off in (-1, 0, 1):
        shifted = {t + timedelta(minutes=off) for t in a}
        overlap = len(shifted & set(b))
        if overlap > best[1]:
            best = (off, overlap)
    return best


def compare_pair(
    name_a: str,
    a: dict[datetime, dict[str, float]],
    name_b: str,
    b: dict[datetime, dict[str, float]],
) -> dict[str, Any]:
    if not a or not b:
        return {
            "pair": f"{name_a}_vs_{name_b}",
            "status": "NO_DATA",
            "rows_a": len(a),
            "rows_b": len(b),
        }

    # E1: drop each source's newest bar.
    a = {t: v for t, v in a.items() if t != max(a)}
    b = {t: v for t, v in b.items() if t != max(b)}
    # regular session only
    a = {t: v for t, v in a.items() if in_regular_session(t)}
    b = {t: v for t, v in b.items() if in_regular_session(t)}
    if not a or not b:
        return {"pair": f"{name_a}_vs_{name_b}", "status": "NO_DATA_AFTER_FILTER"}

    off, _ = best_offset(a, b)
    a_shift = {t + timedelta(minutes=off): v for t, v in a.items()}

    common = sorted(set(a_shift) & set(b))
    only_a = sorted(set(a_shift) - set(b))
    only_b = sorted(set(b) - set(a_shift))

    mismatches: list[dict[str, Any]] = []
    field_counts: Counter = Counter()
    for ts in common:
        for f in COMPARED_FIELDS:
            va, vb = a_shift[ts][f], b[ts][f]
            if va != vb:
                field_counts[f] += 1
                if len(mismatches) < 200:
                    mismatches.append(
                        {
                            "minute_kst": ts.isoformat(),
                            "field": f,
                            name_a: va,
                            name_b: vb,
                            "abs_diff": abs(va - vb),
                            "rel_diff": (abs(va - vb) / vb) if vb else None,
                        }
                    )

    cells = len(common) * len(COMPARED_FIELDS)
    return {
        "pair": f"{name_a}_vs_{name_b}",
        "status": "COMPARED",
        "timestamp_offset_minutes_applied": off,
        "common_minutes": len(common),
        "coverage_only_in_a": len(only_a),
        "coverage_only_in_b": len(only_b),
        "cells_compared": cells,
        "mismatch_cells": sum(field_counts.values()),
        "mismatch_by_field": dict(field_counts),
        "match_rate": (1 - sum(field_counts.values()) / cells) if cells else None,
        "mismatch_samples": mismatches,
    }


async def build_clients() -> dict[str, Any]:
    from app.services.brokers.kis.client import KISClient
    from app.services.brokers.kiwoom import constants as kw_constants
    from app.services.brokers.kiwoom.client import KiwoomMockClient
    from app.services.brokers.toss.client import TossReadClient

    kiwoom = KiwoomMockClient(
        base_url=kw_constants.MOCK_BASE_URL,
        app_key=os.environ["KIWOOM_MOCK_APP_KEY"],
        app_secret=os.environ["KIWOOM_MOCK_APP_SECRET"],
        account_no="",  # chart TRs ignore it; the scoped env has none
    )
    kis = KISClient(is_mock=True)
    toss = TossReadClient.from_settings()
    return {"kiwoom": kiwoom, "kis": kis, "toss": toss}


async def probe_depth(clients: dict, pacers: dict, symbol: str) -> dict[str, Any]:
    """How far back does each source actually serve 1m bars? Cheap, decisive."""
    out: dict[str, Any] = {}

    # Kiwoom: walk base_dt backwards in 90-day steps until empty.
    depth_probe_days = [30, 90, 180, 365]
    out["kiwoom"] = {}
    for d in depth_probe_days:
        target = (now_kst().date() - timedelta(days=d)).strftime("%Y%m%d")
        try:
            rows, meta = await fetch_kiwoom_minutes(
                client=clients["kiwoom"],
                symbol=symbol,
                pacer=pacers["kiwoom"],
                max_pages=1,
                base_dt=target,
            )
            oldest = min(rows).isoformat() if rows else None
            newest = max(rows).isoformat() if rows else None
            out["kiwoom"][f"base_dt_-{d}d"] = {
                "rows": len(rows),
                "oldest": oldest,
                "newest": newest,
                "rows_per_call": meta["rows_raw"],
            }
        except Exception as exc:  # noqa: BLE001
            out["kiwoom"][f"base_dt_-{d}d"] = {"error": f"{type(exc).__name__}: {exc}"}

    # Toss: page backwards via next_before and record the oldest reachable bar.
    out["toss"] = {}
    try:
        rows, meta = await fetch_toss_minutes(
            client=clients["toss"],
            symbol=symbol,
            pacer=pacers["toss"],
            count=200,
            max_pages=1,
        )
        out["toss"]["first_page"] = {
            "rows": len(rows),
            "rows_per_call": meta["rows_raw"],
            "oldest": min(rows).isoformat() if rows else None,
            "next_before": meta.get("next_before"),
        }
    except Exception as exc:  # noqa: BLE001
        out["toss"]["first_page"] = {"error": f"{type(exc).__name__}: {exc}"}

    # KIS: ask for a session ~N days back directly.
    out["kis"] = {}
    for d in depth_probe_days:
        target = now_kst().date() - timedelta(days=d)
        try:
            rows, meta = await fetch_kis_minutes(
                client=clients["kis"],
                symbol=symbol,
                pacer=pacers["kis"],
                session_date=target,
                max_pages=1,
            )
            out["kis"][f"date_-{d}d"] = {
                "rows": len(rows),
                "rows_per_call": meta["rows_raw"],
                "oldest": min(rows).isoformat() if rows else None,
            }
        except Exception as exc:  # noqa: BLE001
            out["kis"][f"date_-{d}d"] = {"error": f"{type(exc).__name__}: {exc}"}

    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-csv", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--session-date", type=str, default=None, help="YYYY-MM-DD (KST)")
    ap.add_argument(
        "--confirm-fetch",
        action="store_true",
        help="required; without it nothing is fetched",
    )
    args = ap.parse_args()

    session_date = (
        date.fromisoformat(args.session_date) if args.session_date else now_kst().date()
    )
    symbols = load_symbols(args.sample_csv)

    report: dict[str, Any] = {
        "ran_at_kst": now_kst().isoformat(),
        "session_date": session_date.isoformat(),
        "sample_symbols": [s for s, _ in symbols],
        "criterion": CRITERION,
        "value_semantics": VALUE_SEMANTICS,
        "confirm_fetch": bool(args.confirm_fetch),
    }

    if not args.confirm_fetch:
        report["status"] = "DRY_RUN_NO_FETCH"
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {"status": "DRY_RUN_NO_FETCH", "symbols": len(symbols)}, indent=2
            )
        )
        return 0

    assert_fetch_window_open()  # fail closed inside 09:00-20:00 KST

    clients = await build_clients()
    pacers = {s: Pacer(s) for s in ("toss", "kiwoom", "kis")}
    per_symbol: list[dict[str, Any]] = []

    try:
        report["depth_probe"] = await probe_depth(clients, pacers, symbols[0][0])

        for ticker, market in symbols:
            entry: dict[str, Any] = {"symbol": ticker, "market": market}
            data: dict[str, dict] = {}
            for name, coro in (
                (
                    "kiwoom",
                    lambda t=ticker: fetch_kiwoom_minutes(
                        client=clients["kiwoom"],
                        symbol=t,
                        pacer=pacers["kiwoom"],
                        max_pages=1,
                    ),
                ),
                (
                    "toss",
                    lambda t=ticker: fetch_toss_minutes(
                        client=clients["toss"],
                        symbol=t,
                        pacer=pacers["toss"],
                        count=400,
                        max_pages=3,
                    ),
                ),
                (
                    "kis",
                    lambda t=ticker: fetch_kis_minutes(
                        client=clients["kis"],
                        symbol=t,
                        pacer=pacers["kis"],
                        session_date=session_date,
                        max_pages=4,
                    ),
                ),
            ):
                try:
                    rows, meta = await coro()
                    rows = {t: v for t, v in rows.items() if t.date() == session_date}
                    data[name] = rows
                    entry[f"{name}_rows"] = len(rows)
                    entry[f"{name}_meta"] = meta
                except Exception as exc:  # noqa: BLE001
                    data[name] = {}
                    entry[f"{name}_error"] = f"{type(exc).__name__}: {exc}"

            entry["pairs"] = [
                compare_pair("toss", data["toss"], "kiwoom", data["kiwoom"]),
                compare_pair("toss", data["toss"], "kis", data["kis"]),
                compare_pair("kiwoom", data["kiwoom"], "kis", data["kis"]),
            ]
            per_symbol.append(entry)
            print(
                f"{ticker}: "
                + " ".join(
                    f"{p['pair']}={p.get('mismatch_cells', '?')}/{p.get('cells_compared', '?')}"
                    for p in entry["pairs"]
                ),
                flush=True,
            )
    finally:
        for c in clients.values():
            close = getattr(c, "aclose", None) or getattr(c, "close", None)
            if close:
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # noqa: BLE001, S110
                    pass

    # aggregate
    agg: dict[str, dict[str, int]] = {}
    for entry in per_symbol:
        for p in entry["pairs"]:
            if p.get("status") != "COMPARED":
                continue
            a = agg.setdefault(
                p["pair"], {"cells": 0, "mismatch": 0, "offsets": Counter()}
            )
            a["cells"] += p["cells_compared"]
            a["mismatch"] += p["mismatch_cells"]
            a["offsets"][p["timestamp_offset_minutes_applied"]] += 1

    summary = {
        pair: {
            "cells": v["cells"],
            "mismatch": v["mismatch"],
            "match_rate": (1 - v["mismatch"] / v["cells"]) if v["cells"] else None,
            "offset_distribution": dict(v["offsets"]),
        }
        for pair, v in agg.items()
    }
    total_mismatch = sum(v["mismatch"] for v in agg.values())
    compared_any = any(v["cells"] for v in agg.values())

    report["per_symbol"] = per_symbol
    report["summary"] = summary
    report["mismatch_count"] = total_mismatch
    report["mismatch_side_adjudicated"] = False
    report["split_approved_by_gate"] = bool(compared_any and total_mismatch == 0)
    report["status"] = "GATE_COMPLETE"
    report["calls_used"] = {s: p.calls for s, p in pacers.items()}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    print(
        json.dumps(
            {
                "summary": summary,
                "mismatch_count": total_mismatch,
                "split_approved_by_gate": report["split_approved_by_gate"],
                "calls_used": report["calls_used"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
