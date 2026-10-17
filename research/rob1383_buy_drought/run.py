"""Regenerate every ROB-1383 v1 (brief rev2) result table.

    uv run python -m research.rob1383_buy_drought.run --inputs <inputs dir>

Prices are cached under ``research/rob1383_buy_drought/.cache/`` (gitignored).
``--offline`` refuses network and uses the cache only (a cache miss becomes a
recorded missing row, never a silent drop). Method notes for each table live
next to the CSVs in ``results/*.md``.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd

from research.rob1383_buy_drought import cohort, funnel
from research.rob1383_buy_drought import q1_support_gate as q1
from research.rob1383_buy_drought import q2_q3_orders as q23
from research.rob1383_buy_drought import q4_excess as q4
from research.rob1383_buy_drought.core import classify_deep_limit_out
from research.rob1383_buy_drought.inputs import load_rejections, load_toss_orders
from research.rob1383_buy_drought.prices import DEFAULT_CACHE_DIR, PriceStore

RESULTS = Path(__file__).resolve().parent / "results"
MARKETS = ("kr", "us", "crypto")
NO_CRYPTO_ORDERS = (
    "not_computable: the inputs hold no crypto orders "
    "(03/toss_orders.json is Toss KR/US only; Upbit orders were not exported)"
)


def _write(frame: pd.DataFrame, name: str, out: Path) -> None:
    path = out / name
    frame.to_csv(path, index=False, float_format="%.6g")
    print(f"wrote {path} ({len(frame)} rows)")


def deep_limit_reason_counts(rejections, enriched: pd.DataFrame):
    flagged = [r for r in rejections if classify_deep_limit_out(r.reason)]
    listing = pd.DataFrame(
        [
            {
                "row": r.row,
                "market": r.market,
                "symbol": r.symbol,
                "created_at_utc": r.created_at.isoformat(),
                "reason_excerpt": r.reason[:240].replace("\n", " "),
            }
            for r in flagged
        ]
    )
    e = enriched.set_index("row")
    rows = []
    for market in MARKETS:
        cohort_rows = [r for r in rejections if r.market == market]
        hits = [r for r in flagged if r.market == market]
        rec = e.loc[[r.row for r in hits]] if hits else e.iloc[0:0]
        rows.append(
            {
                "market": market,
                "n_cohort": len(cohort_rows),
                "n_reason_deep_limit_out": len(hits),
                "share_of_cohort": len(hits) / len(cohort_rows)
                if cohort_rows
                else float("nan"),
                "n_symbols": len({r.symbol for r in hits}),
                "n_support_only_fail": int(rec["support_only_fail"].sum())
                if len(rec)
                else 0,
                "n_recomputed_shallow_only": int(
                    (
                        rec["failed_gates_recomputed"].fillna("")
                        == "deep_limit_band_shallow"
                    ).sum()
                )
                if len(rec)
                else 0,
                "n_recomputed_cohort_shallow_only": int(
                    (
                        enriched[enriched["market"] == market][
                            "failed_gates_recomputed"
                        ].fillna("")
                        == "deep_limit_band_shallow"
                    ).sum()
                ),
            }
        )
    return pd.DataFrame(rows), listing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    rejections = load_rejections(args.inputs)
    orders = load_toss_orders(args.inputs)
    store = PriceStore(cache_dir=args.cache_dir, offline=args.offline)

    # ---- §1 cohort enrichment (one point-in-time reconstruction per row)
    recon = q1.reconstruct_all(rejections, store)
    enriched = cohort.build(rejections, recon, store)
    _write(enriched, "01_cohort_enriched.csv", args.out)
    episodes = dict(zip(enriched["row"], enriched["parent_episode_id"], strict=True))

    # ---- funnel
    for market, table in funnel.summarise(args.inputs).items():
        _write(table, f"funnel_{market}.csv", args.out)

    # ---- Q1
    q1_detail = q1.run(rejections, store, recon, enriched)
    _write(q1_detail, "q1_detail.csv", args.out)
    for market in MARKETS:
        _write(q1.summarise(q1_detail, market), f"q1_{market}.csv", args.out)
    recon_prior = q1.reconstruct_all(
        rejections, store, price_mode="prior_close", reuse=recon
    )
    _write(
        q1.agreement(enriched, rejections, recon_prior),
        "q1_reconstruction_agreement.csv",
        args.out,
    )
    counts, listing = deep_limit_reason_counts(rejections, enriched)
    _write(counts, "q1_deep_limit_reason_counts.csv", args.out)
    _write(listing, "q1_deep_limit_reason_rows.csv", args.out)

    # ---- Q2 / Q3
    q23_detail = q23.run(orders, store)
    _write(q23_detail, "q2_q3_detail.csv", args.out)
    _write(q23.coverage(q23_detail), "q2_q3_coverage.csv", args.out)
    for market in ("kr", "us"):
        _write(q23.summarise_q2(q23_detail, market), f"q2_{market}.csv", args.out)
        _write(
            q23.q2_distribution(q23_detail, market),
            f"q2_{market}_distribution.csv",
            args.out,
        )
        _write(q23.summarise_q3(q23_detail, market), f"q3_{market}.csv", args.out)
    for q in ("q2", "q3"):
        _write(
            pd.DataFrame([{"market": "crypto", "status": NO_CRYPTO_ORDERS}]),
            f"{q}_crypto.csv",
            args.out,
        )

    # ---- Q4
    q4_detail = q4.run(rejections, store, episodes)
    _write(q4_detail, "q4_detail.csv", args.out)
    _write(q4.exclusions(q4_detail), "q4_exclusions.csv", args.out)
    for market in MARKETS:
        _write(q4.summarise(q4_detail, market), f"q4_{market}.csv", args.out)
        _write(
            q4.robustness(q4_detail, market), f"q4_{market}_robustness.csv", args.out
        )

    # ---- data ledgers
    missing = pd.DataFrame(
        [
            {"market": m, "symbol": s, "reason": r}
            for (m, s), r in sorted(store.missing.items())
        ],
        columns=["market", "symbol", "reason"],
    )
    _write(missing, "price_missing.csv", args.out)
    hourly_missing = pd.DataFrame(
        [
            {"market": m, "symbol": s, "reason": r}
            for (m, s), r in sorted(store.hourly_missing.items())
        ],
        columns=["market", "symbol", "reason"],
    )
    _write(hourly_missing, "price_hourly_missing.csv", args.out)
    splits = pd.DataFrame(
        [
            {"market": m, "symbol": s, "split_dates": "|".join(d)}
            for (m, s), d in sorted(store.split_events.items())
        ],
        columns=["market", "symbol", "split_dates"],
    )
    _write(splits, "price_split_events.csv", args.out)
    print(f"done in {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
