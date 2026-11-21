from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from research.upbit_intraday import data as D
from research.upbit_intraday import fetch as F
from research.upbit_intraday import rules as R
from research.upbit_intraday import run as RUN

N = (R.epoch(R.DATA_END_EXCLUSIVE_UTC) - R.epoch(R.DATA_START_UTC)) // R.BAR_SECONDS
NAMES = ["KRW-BTC", "KRW-ETH"] + [f"KRW-M{i:02d}" for i in range(14)]


def synthetic_root(tmp: Path, *, test_seed: int = 0) -> Path:
    """Random-walk candles with volume bursts; the test half (and later) is a
    function of ``test_seed`` only, the earlier data are fixed."""
    start = R.DATA_START_UTC
    cut = (R.epoch(R.TEST_START) - R.epoch(R.DATA_START_UTC)) // R.BAR_SECONDS
    rng_a = np.random.default_rng(11)
    rng_b = np.random.default_rng(1000 + test_seed)
    (tmp / "candles15").mkdir(parents=True)
    entries = {}
    for name in NAMES:
        shocks = np.concatenate(
            [rng_a.normal(0, 0.004, cut), rng_b.normal(0, 0.004, N - cut)]
        )
        qn = np.concatenate(
            [rng_a.lognormal(0, 0.9, cut), rng_b.lognormal(0, 0.9, N - cut)]
        )
        drop = np.concatenate([rng_a.random(cut) < 0.02, rng_b.random(N - cut) < 0.02])
        price = 1000.0 * np.exp(np.cumsum(shocks))
        rows = []
        prev = price[0]
        for i in range(N):
            if drop[i]:
                prev = price[i]
                continue
            op, cl = prev, price[i]
            hi, lo = max(op, cl) * 1.001, min(op, cl) * 0.999
            when = (start + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            rows.append(
                F.Row(
                    when,
                    f"{op:.4f}",
                    f"{hi:.4f}",
                    f"{lo:.4f}",
                    f"{cl:.4f}",
                    "1.0",
                    f"{40e6 * qn[i]:.2f}",
                )
            )
            prev = cl
        blob = F.rows_to_gzip_csv(rows)
        (tmp / "candles15" / f"{name}.csv.gz").write_bytes(blob)
        entries[name] = {
            "market": name,
            "file": f"candles15/{name}.csv.gz",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "rows": len(rows),
            "first_time_utc": rows[0].time_utc,
            "last_time_utc": rows[-1].time_utc,
            "pages": 1,
            "page_body_sha256": ["0" * 64],
        }
    market_all = json.dumps(
        [
            {
                "market": n,
                "market_event": {"warning": n == "KRW-M03", "caution": {"X": False}},
            }
            for n in NAMES
        ]
    ).encode()
    (tmp / "market_all.json").write_bytes(market_all)
    manifest = {
        "manifest_format": F.MANIFEST_FORMAT,
        "dataset": "synthetic",
        "window": {
            "start_utc": F.iso_z(R.DATA_START_UTC),
            "end_exclusive_utc": F.iso_z(R.DATA_END_EXCLUSIVE_UTC),
            "bar_seconds": 900,
        },
        "market_list": {
            "file": "market_all.json",
            "sha256": hashlib.sha256(market_all).hexdigest(),
            "fetched_at": "x",
            "krw_market_count": len(NAMES),
            "url": "x",
        },
        "markets": entries,
        "failures": {},
    }
    (tmp / "manifest.json").write_bytes(F.canonical_json(manifest))
    return tmp


@pytest.fixture(scope="module")
def root_a(tmp_path_factory) -> Path:
    return synthetic_root(tmp_path_factory.mktemp("a"), test_seed=0)


def test_develop_never_reads_the_test_half(root_a: Path, tmp_path_factory):
    root_b = synthetic_root(tmp_path_factory.mktemp("b"), test_seed=1)
    a = RUN.develop(root_a)
    b = RUN.develop(root_b)
    assert (
        a["records"] == b["records"]
    )  # develop outcomes identical if the test half changes
    assert a["selection"]["selected"] == b["selection"]["selected"]
    assert any(
        v is not None for v in a["selection"]["selected"].values()
    )  # synthetic data selects something


def test_test_look_is_single_shot_and_checks_the_frozen_inputs(
    root_a: Path, tmp_path: Path
):
    out = RUN.develop(root_a)
    sel_path = tmp_path / "selection.json"
    sel_path.write_text(json.dumps(out["selection"]))
    results = RUN.test_look(root_a, sel_path)
    assert set(results["verdicts"]) == set(R.FAMILIES)
    assert all(
        v["verdict"] in {"SUPPORTED", "NOT_SUPPORTED", "INSUFFICIENT_SAMPLE"}
        for v in results["verdicts"].values()
    )
    assert len(results["grid_test_records"]) == 12 * len(R.HORIZONS)
    assert results["data"]["n_markets"] == len(NAMES)
    with pytest.raises(SystemExit, match="already opened"):
        RUN.test_look(root_a, sel_path)
    (root_a / "test_look.done").unlink()
    tampered = json.loads(sel_path.read_text())
    tampered["code_sha256"]["rules.py"] = "0" * 64
    sel_path.write_text(json.dumps(tampered))
    with pytest.raises(SystemExit, match="frozen code changed"):
        RUN.test_look(root_a, sel_path)
    tampered["code_sha256"] = RUN.frozen_code_hashes()
    tampered["dataset_manifest_sha256"] = "1" * 64
    sel_path.write_text(json.dumps(tampered))
    with pytest.raises(SystemExit, match="different dataset"):
        RUN.test_look(root_a, sel_path)


def test_warning_filter_variant_excludes_flagged_markets(root_a: Path):
    panel, _ = D.load_panel(root_a)
    flagged = D.warning_markets(root_a)
    assert flagged == {"KRW-M03"}
    ctx = R.build_context(panel, exclude_markets=flagged)
    i = panel.markets.index("KRW-M03")
    assert not ctx.active[i] and not ctx.eligible[i].any()
    sset = R.make_signal_set(ctx, R.Config("A_breakout", 16, 3.0))
    assert i not in set(sset.mi.tolist()) and i not in set(sset.b_mi.tolist())


def test_report_tables_render_from_results(root_a: Path, tmp_path: Path):
    from research.upbit_intraday import report

    out = RUN.develop(root_a)
    sel = tmp_path / "selection.json"
    sel.write_text(json.dumps(out["selection"]))
    guard = root_a / "test_look.done"
    if guard.exists():
        guard.unlink()
    results = RUN.test_look(root_a, sel)
    text = "\n".join(
        [
            report.verdict_table(results),
            report.grid_table(out["records"], "dev"),
            report.grid_table(results["grid_test_records"], "test"),
            report.horizon_table(results),
            report.bucket_table(results),
            report.sensitivity_table(results),
            report.listing_table(results),
            report.counts_table(results),
        ]
    )
    assert "A 신규 추세 돌파" in text and text.count("\n") > 20
    assert "nan" not in text.lower().replace("fan", "")


def test_case_day_lists_frozen_signals_on_the_retrospective_day(tmp_path: Path):
    start, end = RUN.CASE_START, RUN.CASE_END_EXCLUSIVE
    n = int((end - start).total_seconds() // 900)
    (tmp_path / "candles15").mkdir()
    entries = {}
    rng = np.random.default_rng(5)
    for name in ["KRW-AAA", "KRW-BBB", "KRW-CCC", "KRW-DDD", "KRW-EEE", "KRW-BTC"]:
        rows = []
        for i in range(n):
            when = (start + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            price = 100.0
            q = 40e6
            if name == "KRW-AAA" and i == 150:  # a breakout bar on the case day
                price, q = 108.0, 400e6
            rows.append(
                F.Row(
                    when,
                    "100.0",
                    f"{max(price, 100) * 1.001:.4f}",
                    "99.9",
                    f"{price:.4f}",
                    "1.0",
                    f"{q * rng.uniform(0.95, 1.05):.2f}",
                )
            )
        blob = F.rows_to_gzip_csv(rows)
        (tmp_path / "candles15" / f"{name}.csv.gz").write_bytes(blob)
        entries[name] = {
            "market": name,
            "file": f"candles15/{name}.csv.gz",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "rows": n,
            "first_time_utc": rows[0].time_utc,
            "last_time_utc": rows[-1].time_utc,
            "pages": 1,
            "page_body_sha256": ["0" * 64],
        }
    market_all = b"[]"
    (tmp_path / "market_all.json").write_bytes(market_all)
    manifest = {
        "manifest_format": F.MANIFEST_FORMAT,
        "window": {
            "start_utc": F.iso_z(start),
            "end_exclusive_utc": F.iso_z(end),
            "bar_seconds": 900,
        },
        "market_list": {
            "file": "market_all.json",
            "sha256": hashlib.sha256(market_all).hexdigest(),
        },
        "markets": entries,
        "failures": {},
    }
    (tmp_path / "manifest.json").write_bytes(F.canonical_json(manifest))
    sel = {
        "selected": {
            "A_breakout": {"family": "A_breakout", "n": 16, "x": 3.0},
            "B_held_rebound": None,
            "C_downside": None,
        }
    }
    sel_path = tmp_path / "sel.json"
    sel_path.write_text(json.dumps(sel))
    out = RUN.case_day(tmp_path, sel_path)
    assert out["configs"]["A_breakout"]["signals_from_2026-11-21_09:00_kst"] == 1
    sig = out["signals"][0]
    assert (
        sig["market"] == "KRW-AAA"
        and sig["family"] == "A_breakout"
        and sig["net_4"] is not None
    )
