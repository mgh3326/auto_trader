"""4h aggregation, rolling-Q0.75 tail definition, and incidence accounting.

EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC

Two components, both derived from public unsigned data:

  C1 proxy-OFI  volume-weighted signed taker imbalance over the 4h bucket:
                (2*sum(takerBuyQuote) - sum(quoteVolume)) / sum(quoteVolume)
                in [-1, +1]. This is an *approximation* of order-flow imbalance:
                klines only expose aggregate taker-buy volume, so it carries no
                queue/'depth information and cannot distinguish one large taker
                from many small ones. aggTrades (which could) are deliberately
                not collected.

  C2 premium    mean of the 5m premium-index closes inside the bucket
                (perp mark vs spot index). Positive = perp rich to spot.

Bucket completeness follows research/nautilus_scalping/rob974_features.py
``build_complete_4h``: exact UTC boundaries, and a bucket is emitted only when
every constituent bar is present. Incomplete buckets are dropped as explicit
gaps — never forward-filled or interpolated — and are excluded from both the
numerator and the denominator of every incidence figure.

Firing is defined three ways because "2-component rolling-Q0.75 tail" is
genuinely ambiguous and incidence is highly sensitive to which reading is meant.
Reporting a single number would hide that sensitivity:

  D1 CONJUNCTION  both components at/above their own rolling Q0.75
  D2 COMPOSITE    rolling z-sum of the two components at/above its rolling Q0.75
  D3 DISJUNCTION  either component at/above its own rolling Q0.75

No PnL, no returns, no performance metric is computed anywhere in this file.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.dfc_retro_probe.common import (
    ARTIFACT_ROOT,
    BAR_MS,
    FOUR_H_MS,
    LABEL,
    SYMBOLS,
    label_metadata,
)

RAW_DIR = ARTIFACT_ROOT / "raw"
BARS_PER_BUCKET = FOUR_H_MS // BAR_MS  # 48
BUCKETS_PER_DAY = 6
QUANTILE = 0.75

# Rolling window is expressed in *valid 4h buckets*, recomputed every bucket.
PRIMARY_WINDOW = 180  # ~30 days of 4h buckets
SENSITIVITY_WINDOWS = (180, 540)  # ~30d, ~90d


@dataclass
class SymbolFrame:
    symbol: str
    frame: pd.DataFrame
    total_buckets_seen: int
    gap_buckets: int


def build_4h(symbol: str) -> SymbolFrame:
    """Aggregate 5m klines + premium into complete UTC 4h buckets."""
    klines = pq.read_table(RAW_DIR / f"klines_{symbol}_5m.parquet").to_pandas()
    premium = pq.read_table(RAW_DIR / f"premium_{symbol}_5m.parquet").to_pandas()

    merged = klines.merge(
        premium[["open_time", "close"]].rename(columns={"close": "premium"}),
        on="open_time",
        how="inner",
    )
    merged["bucket"] = (merged["open_time"] // FOUR_H_MS) * FOUR_H_MS

    grouped = merged.groupby("bucket", sort=True)
    agg = grouped.agg(
        bars=("open_time", "size"),
        quote_volume=("quote_volume", "sum"),
        taker_buy_quote=("taker_buy_quote", "sum"),
        premium=("premium", "mean"),
    ).reset_index()

    total_seen = len(agg)
    # Completeness rule mirrors build_complete_4h: all constituent bars present.
    complete = agg[(agg["bars"] == BARS_PER_BUCKET) & (agg["quote_volume"] > 0)].copy()
    gaps = total_seen - len(complete)

    complete["proxy_ofi"] = (
        2.0 * complete["taker_buy_quote"] - complete["quote_volume"]
    ) / complete["quote_volume"]
    complete["symbol"] = symbol
    complete["bucket_utc"] = pd.to_datetime(complete["bucket"], unit="ms", utc=True)
    complete = complete.sort_values("bucket").reset_index(drop=True)

    return SymbolFrame(symbol, complete, total_seen, gaps)


def add_firing(
    frame: pd.DataFrame, window: int, quantile: float = QUANTILE
) -> pd.DataFrame:
    """Attach rolling-Q0.75 tail flags for all three firing definitions.

    ``shift(1)`` guarantees the current bucket never contributes to its own
    threshold; ``min_periods=window`` means the first `window` buckets produce no
    signal at all rather than a signal from a partial window.
    """
    out = frame.copy()
    for col in ("proxy_ofi", "premium"):
        past = out[col].shift(1)
        out[f"{col}_q75"] = past.rolling(window, min_periods=window).quantile(quantile)
        mean = past.rolling(window, min_periods=window).mean()
        std = past.rolling(window, min_periods=window).std(ddof=0)
        out[f"{col}_z"] = (out[col] - mean) / std.replace(0.0, pd.NA)

    out["fire_ofi"] = out["proxy_ofi"] >= out["proxy_ofi_q75"]
    out["fire_prem"] = out["premium"] >= out["premium_q75"]

    out["composite_z"] = out["proxy_ofi_z"] + out["premium_z"]
    comp_past = out["composite_z"].shift(1)
    out["composite_q75"] = comp_past.rolling(window, min_periods=window).quantile(
        quantile
    )

    # Evaluable = full trailing window available for every threshold used.
    out["evaluable"] = (
        out["proxy_ofi_q75"].notna()
        & out["premium_q75"].notna()
        & out["composite_q75"].notna()
        & out["composite_z"].notna()
    )

    out["D1_conjunction"] = out["evaluable"] & out["fire_ofi"] & out["fire_prem"]
    out["D2_composite"] = out["evaluable"] & (
        out["composite_z"] >= out["composite_q75"]
    )
    out["D3_disjunction"] = out["evaluable"] & (out["fire_ofi"] | out["fire_prem"])
    out.loc[~out["evaluable"], ["fire_ofi", "fire_prem"]] = False
    return out


DEFS = ("D1_conjunction", "D2_composite", "D3_disjunction")


def _rate_block(sub: pd.DataFrame, buckets_per_day: int) -> dict[str, Any]:
    evaluable = int(sub["evaluable"].sum())
    block: dict[str, Any] = {"evaluable_buckets": evaluable}
    for definition in DEFS:
        fires = int(sub[definition].sum())
        block[definition] = {
            "fires": fires,
            "fire_rate_per_bucket": round(fires / evaluable, 6) if evaluable else None,
            "incidence_per_day": round(fires / evaluable * buckets_per_day, 4)
            if evaluable
            else None,
        }
    return block


def summarize(panel: pd.DataFrame, window: int) -> dict[str, Any]:
    basket_buckets_per_day = BUCKETS_PER_DAY * len(SYMBOLS)

    result: dict[str, Any] = {
        "admissibility": LABEL,
        "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC": True,
        "window_definition": {
            "rolling_window_buckets": window,
            "rolling_window_days_approx": round(window / BUCKETS_PER_DAY, 1),
            "quantile": QUANTILE,
            "recompute_frequency": "every 4h bucket",
            "lookahead_guard": "shift(1) — current bucket excluded from its own threshold",
            "min_periods": window,
            "window_units": "valid 4h buckets (gap buckets dropped, not filled)",
        },
        "basket": _rate_block(panel, basket_buckets_per_day),
        "by_symbol": {},
        "by_month": {},
    }

    for symbol in SYMBOLS:
        sub = panel[panel["symbol"] == symbol]
        result["by_symbol"][symbol] = _rate_block(sub, BUCKETS_PER_DAY)

    panel = panel.copy()
    panel["month"] = panel["bucket_utc"].dt.strftime("%Y-%m")
    for month, sub in panel.groupby("month"):
        result["by_month"][month] = _rate_block(sub, basket_buckets_per_day)

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=PRIMARY_WINDOW)
    args = parser.parse_args()

    frames: list[SymbolFrame] = [build_4h(s) for s in SYMBOLS]
    coverage = {
        f.symbol: {
            "buckets_seen": f.total_buckets_seen,
            "complete_buckets": len(f.frame),
            "gap_buckets_dropped": f.gap_buckets,
            "first_bucket_utc": f.frame["bucket_utc"].iloc[0].isoformat(),
            "last_bucket_utc": f.frame["bucket_utc"].iloc[-1].isoformat(),
            "span_days": round(
                (f.frame["bucket"].iloc[-1] - f.frame["bucket"].iloc[0])
                / (FOUR_H_MS * BUCKETS_PER_DAY),
                2,
            ),
        }
        for f in frames
    }

    report: dict[str, Any] = {
        "admissibility": LABEL,
        "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC": True,
        "purpose": "RETRO_INCIDENCE_ONLY",
        "auth": "NONE",
        "signed_endpoint_calls": 0,
        "aggtrades_used": "NO",
        "forward_fill_used": "NO",
        "pnl_or_performance_computed": "NO",
        "contract_3_04_to_3_83_adjudicated": "NO",
        "components": {
            "C1": "proxy-OFI = (2*sum(takerBuyQuote) - sum(quoteVolume)) / sum(quoteVolume)",
            "C2": "premium = mean of 5m premiumIndex closes in bucket",
            "missing_component": "open interest (OI) — not collected, proxy is incomplete",
        },
        "coverage": coverage,
        "gaps_total": sum(f.gap_buckets for f in frames),
        "windows": {},
    }

    panels: dict[int, pd.DataFrame] = {}
    for window in SENSITIVITY_WINDOWS:
        panel = pd.concat(
            [add_firing(f.frame, window) for f in frames], ignore_index=True
        )
        panels[window] = panel
        report["windows"][str(window)] = summarize(panel, window)

    report["primary_window"] = str(args.window)

    # Trailing 390d slice — the brief's stated floor, reported separately so the
    # figure most comparable to a recent-regime claim is explicit.
    primary_panel = panels[args.window]
    cutoff = primary_panel["bucket_utc"].max() - pd.Timedelta(days=390)
    trailing = primary_panel[primary_panel["bucket_utc"] >= cutoff]
    report["trailing_390d"] = {
        "cutoff_utc": cutoff.isoformat(),
        "window": args.window,
        **summarize(trailing, args.window),
    }

    # Inversion: which threshold quantile reproduces a given per-day incidence?
    # This is an observation about the definition's sensitivity, NOT a judgement
    # about whether any external contract figure is correct.
    sweep: dict[str, Any] = {}
    for q in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        panel_q = pd.concat(
            [add_firing(f.frame, args.window, q) for f in frames], ignore_index=True
        )
        block = _rate_block(panel_q, BUCKETS_PER_DAY * len(SYMBOLS))
        sweep[f"{q:.2f}"] = {d: block[d]["incidence_per_day"] for d in DEFS}
    report["threshold_quantile_sweep"] = {
        "note": (
            "Basket incidence per day as the tail quantile varies, window="
            f"{args.window} buckets. Provided so a reader can see how strongly "
            "incidence depends on the threshold choice. No adjudication."
        ),
        "sweep": sweep,
    }

    out_json = ARTIFACT_ROOT / "incidence_report.json"
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Bucket-level panel, label stamped into parquet metadata.
    primary = panels[args.window]
    keep = [
        "symbol",
        "bucket",
        "bucket_utc",
        "bars",
        "quote_volume",
        "taker_buy_quote",
        "proxy_ofi",
        "premium",
        "proxy_ofi_q75",
        "premium_q75",
        "composite_z",
        "composite_q75",
        "evaluable",
        *DEFS,
    ]
    table = pa.Table.from_pandas(primary[keep], preserve_index=False)
    meta = dict(table.schema.metadata or {})
    meta.update(label_metadata({"rolling_window_buckets": args.window}))
    table = table.replace_schema_metadata(meta)
    pq.write_table(table, ARTIFACT_ROOT / "buckets_4h.parquet", compression="zstd")

    print(json.dumps({k: report[k] for k in ("coverage", "gaps_total")}, indent=2))
    for window in SENSITIVITY_WINDOWS:
        basket = report["windows"][str(window)]["basket"]
        print(f"\n--- window={window} buckets (~{window // 6}d) ---")
        for definition in DEFS:
            print(f"  {definition}: {basket[definition]['incidence_per_day']}/day")


if __name__ == "__main__":
    main()
