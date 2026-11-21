"""Develop-half selection and the single test-half look (hk 6035).

``develop`` touches only develop-half outcomes and writes the frozen selection.
``test`` refuses to run twice (a second run is another look and must be counted
in the registry first) and writes ``results.json`` with every aggregate the
report needs. Raw candles and per-signal rows stay in the artifact directory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from . import data as D
from . import evaluate as E
from . import rules as R

SELECTION_FORMAT = "upbit-intraday-selection/v1"
KST = timedelta(hours=9)


def _json(obj: Any) -> bytes:
    return (
        json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    ).encode()


def frozen_code_hashes() -> dict[str, str]:
    here = Path(__file__).resolve().parent
    return {
        name: hashlib.sha256((here / name).read_bytes()).hexdigest()
        for name in ("rules.py", "evaluate.py")
    }


def develop(root: Path) -> dict[str, Any]:
    panel, manifest = D.load_panel(root)
    ctx = R.build_context(panel)
    sets = {cfg.id: R.make_signal_set(ctx, cfg) for cfg in R.all_configs()}
    records: list[dict] = []
    for cfg in R.all_configs():
        records += E.evaluate_config(ctx, sets[cfg.id], "dev")
    selection = {
        "format": SELECTION_FORMAT,
        "dataset_manifest_sha256": D.manifest_sha256(root),
        "code_sha256": frozen_code_hashes(),
        "rule": "largest develop-half mean excess over control B at 4h, base cost, n_b >= "
        f"{E.MIN_N_SELECT}; ties larger N then larger X",
        "selected": {f: E.select_config(records, f) for f in R.FAMILIES},
    }
    (root / "dev_records.json").write_bytes(_json(records))
    (root / "selection.json").write_bytes(_json(selection))
    return {"selection": selection, "records": records}


def _config(sel: dict) -> R.Config:
    return R.Config(sel["family"], int(sel["n"]), float(sel["x"]))


def _signal_listing(ctx: R.Context, sset: R.SignalSet, half: str, h: int) -> dict:
    cfg = sset.config
    net = ctx.net(h, 0, cfg.direction, R.PRIMARY_SCENARIO)[sset.mi, sset.s0]
    sel = np.flatnonzero((sset.half == half) & np.isfinite(net))
    if len(sel) == 0:
        return {"worst": [], "best": [], "n_markets": 0, "top5_market_share": None}
    order = sel[np.argsort(net[sel])]

    def row(k: int) -> dict:
        t = (
            datetime.fromtimestamp(ctx.panel.entry_time_epoch(int(sset.s0[k])), UTC)
            + KST
        )
        return {
            "market": ctx.panel.markets[int(sset.mi[k])],
            "entry_kst": t.strftime("%Y-%m-%d %H:%M"),
            "net": float(net[k]),
        }

    counts = np.bincount(sset.mi[sel])
    top5 = np.sort(counts)[::-1][:5].sum() / counts.sum()
    return {
        "worst": [row(int(k)) for k in order[:5]],
        "best": [row(int(k)) for k in order[::-1][:5]],
        "n_markets": int((counts > 0).sum()),
        "top5_market_share": float(top5),
    }


def _primary(extras: list[dict], family: str, variant: str) -> dict | None:
    for rec in extras:
        if (
            rec["variant"] == variant
            and rec["family"] == family
            and rec["half"] == "test"
            and rec["h"] == R.PRIMARY_H
            and rec["delay"] == 0
            and rec["scenario"] == R.PRIMARY_SCENARIO
            and rec["bucket"] is None
        ):
            return rec
    return None


def test_look(
    root: Path, selection_path: Path, *, allow_additional_look: bool = False
) -> dict[str, Any]:
    guard = root / "test_look.done"
    if guard.exists() and not allow_additional_look:
        raise SystemExit(
            "test half already opened once; another look must be counted in the registry first"
        )
    selection = json.loads(selection_path.read_text())
    if selection.get("format") != SELECTION_FORMAT:
        raise SystemExit("unexpected selection file")
    if selection["dataset_manifest_sha256"] != D.manifest_sha256(root):
        raise SystemExit("selection was made on a different dataset")
    if selection["code_sha256"] != frozen_code_hashes():
        raise SystemExit("frozen code changed since the selection")

    panel, _ = D.load_panel(root)
    flagged = D.warning_markets(root)
    ctx = R.build_context(panel)
    ctx_f = R.build_context(panel, exclude_markets=flagged)
    sets = {cfg.id: R.make_signal_set(ctx, cfg) for cfg in R.all_configs()}

    records: list[dict] = []
    for cfg in R.all_configs():  # whole grid, test half, descriptive
        records += E.evaluate_config(ctx, sets[cfg.id], "test")

    selected: dict[str, Any] = {}
    extras: list[dict] = []
    verdicts: dict[str, Any] = {}
    verdicts_filtered: dict[str, Any] = {}
    listings: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    for family in R.FAMILIES:
        sel = selection["selected"][family]
        selected[family] = sel
        if sel is None:
            verdicts[family] = E.verdict(None)
            verdicts_filtered[family] = E.verdict(None)
            continue
        cfg = _config(sel)
        sset = sets[cfg.id]
        sset_f = R.make_signal_set(ctx_f, cfg)
        for half in R.HALVES:
            for variant, c, s in (
                ("all", ctx, sset),
                ("warning_filter", ctx_f, sset_f),
            ):
                for rec in E.evaluate_config(
                    c,
                    s,
                    half,
                    delays=R.DELAYS,
                    scenarios=tuple(R.COSTS),
                    by_bucket=(variant == "all"),
                ):
                    rec["variant"] = variant
                    extras.append(rec)
        listings[family] = _signal_listing(ctx, sset, "test", R.PRIMARY_H)
        diagnostics[family] = {
            "config": cfg.id,
            "entry_missing": sset.entry_missing,
            "thin_control_b": sset.thin_b,
            "thin_control_a": sset.thin_a,
            "n_signals_by_half": {h: int((sset.half == h).sum()) for h in R.HALVES},
        }

        verdicts[family] = E.verdict(_primary(extras, family, "all"))
        verdicts_filtered[family] = E.verdict(
            _primary(extras, family, "warning_filter")
        )

    grid_counts = {
        cfg.id: {
            "dev": int((sets[cfg.id].half == "dev").sum()),
            "test": int((sets[cfg.id].half == "test").sum()),
            "entry_missing": sets[cfg.id].entry_missing,
            "thin_control_b": sets[cfg.id].thin_b,
            "thin_control_a": sets[cfg.id].thin_a,
        }
        for cfg in R.all_configs()
    }
    missing_share = float(np.isnan(panel.c).mean())
    results = {
        "dataset_manifest_sha256": D.manifest_sha256(root),
        "selection": selection,
        "code_sha256": frozen_code_hashes(),
        "data": {
            "n_markets": len(panel.markets),
            "n_slots": panel.n,
            "missing_candle_share": missing_share,
            "warning_or_caution_markets_at_fetch": len(flagged & set(panel.markets)),
        },
        "grid_counts": grid_counts,
        "grid_test_records": records,
        "selected_records": extras,
        "verdicts": verdicts,
        "verdicts_warning_filter": verdicts_filtered,
        "listings": listings,
        "diagnostics": diagnostics,
    }
    (root / "results.json").write_bytes(_json(results))
    guard.write_text(datetime.now(UTC).isoformat() + "\n")
    return results
