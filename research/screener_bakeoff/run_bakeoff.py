"""Screener source bakeoff runner — read-only.

    uv run python -m research.screener_bakeoff.run_bakeoff --out research/screener_bakeoff/artifacts

Writes CSV/JSON artifacts only.  Touches no application table for writing,
no broker, no proposal/watch/order surface.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

import numpy as np
import pandas as pd

from research.screener_bakeoff import indicators as ind
from research.screener_bakeoff import panel as P
from research.screener_bakeoff import scoring
from research.screener_bakeoff import sources as S
from research.screener_bakeoff.spec import (
    CRYPTO_GATE_VARIANTS,
    EXPERIMENT_ID,
    GATE_B_MIN_INDEPENDENT_FAMILIES,
    GATE_RSI_MAX,
    GATE_SUPPORT_WITHIN_PCT,
    GATE_VARIANTS,
    HORIZONS,
    RECENT_WINDOW_CALENDAR_DAYS,
    RECENT_WINDOW_SESSIONS,
    SOURCES,
    SR_WINDOW_BARS,
    TOP_N,
)

SINCE = dt.date(2025, 6, 1)

_KR_BUILDERS = {
    "kr.consecutive_gainers": S.src_consecutive_gainers,
    "kr.high_volume_surge": S.src_high_volume_surge,
    "kr.top_gainers": S.src_top_gainers,
    "kr.top_losers": S.src_top_losers,
    "kr.trade_amount": S.src_trade_amount,
    "kr.investor_flow_momentum": S.src_investor_flow_momentum,
    "kr.double_buy": S.src_double_buy,
    "kr.oversold_recovery": S.src_kr_oversold,
    "kr.cheap_value": S.src_kr_cheap_value,
    "kr.high_yield_value": S.src_kr_high_yield_value,
    "kr.undervalued_breakout": S.src_kr_undervalued_breakout,
    "kr.profitable_company": S.src_kr_profitable_company,
    "kr.undervalued_growth": S.src_kr_undervalued_growth,
    "kr.stable_growth": S.src_kr_stable_growth,
    "kr.growth_expectation_toss": S.src_kr_growth_expectation_toss,
    "kr.steady_dividend": S.src_kr_steady_dividend,
    "kr.future_dividend_king": S.src_kr_future_dividend_king,
}
_US_BUILDERS = {
    "us.consecutive_gainers": S.src_consecutive_gainers,
    "us.high_volume_surge": S.src_high_volume_surge,
    "us.top_gainers": S.src_top_gainers,
    "us.top_losers": S.src_top_losers,
    "us.trade_amount": S.src_trade_amount,
    "us.cheap_value": S.src_us_cheap_value,
    "us.high_yield_value": S.src_us_high_yield_value,
    "us.undervalued_breakout": S.src_us_undervalued_breakout,
    "us.steady_dividend": S.src_us_steady_dividend,
}
_CRYPTO_BUILDERS = {
    "crypto.high_volume": S.src_crypto_high_volume,
    "crypto.oversold": S.src_crypto_oversold,
    "crypto.momentum": S.src_crypto_momentum,
    "crypto.funding_squeeze": S.src_crypto_funding_squeeze,
    "crypto.funding_overheated": S.src_crypto_funding_overheated,
    "crypto.oi_surge": S.src_crypto_oi_surge,
    "crypto.long_short_skew": S.src_crypto_long_short_skew,
    "crypto.tv_rsi45": S.src_crypto_rsi45,
}


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _group_by_date(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    # NOT dict(groupby): pandas GroupBy is not a (key, value) iterable dict() accepts.
    return {d: g for d, g in df.groupby("snapshot_date")}  # noqa: C416


def _rsi_lookup(panel: P.PricePanel) -> dict:
    out: dict = {}
    for sym, closes in panel.close.items():
        series = ind.rsi_wilder_series(closes)
        days = panel.dates[sym]
        for i, value in enumerate(series):
            if not np.isnan(value):
                out[(sym, days[i])] = float(value)
    return out


def build_contexts():
    _log("loading price panels ...")
    kr_prices = P.load_kr_price_panel()
    us_prices = P.load_us_price_panel()
    _log(f"  kr symbols={len(kr_prices.dates)} us symbols={len(us_prices.dates)}")

    _log("loading snapshot panels ...")
    kr_snap = P.load_screener_snapshots("kr", SINCE)
    us_snap = P.load_screener_snapshots("us", SINCE)
    kr_fund = P.load_kr_fundamentals(SINCE)
    us_val = P.load_us_valuation(SINCE)
    flow = P.load_investor_flow(SINCE)
    crypto_snap = P.load_crypto_snapshots(SINCE)
    crypto_prices = P.load_crypto_price_panel(crypto_snap)
    _log(
        f"  kr_snap={len(kr_snap)} us_snap={len(us_snap)} kr_fund={len(kr_fund)} "
        f"us_val={len(us_val)} flow={len(flow)} crypto={len(crypto_snap)}"
    )

    kr = S.MarketContext(
        "kr",
        _group_by_date(kr_snap),
        _group_by_date(kr_fund),
        _group_by_date(flow),
        {},
        kr_prices,
        kr_prices.calendar,
    )
    us = S.MarketContext(
        "us",
        _group_by_date(us_snap),
        _group_by_date(us_val),
        {},
        {},
        us_prices,
        us_prices.calendar,
    )
    crypto = S.MarketContext(
        "crypto",
        {},
        {},
        {},
        _group_by_date(crypto_snap),
        crypto_prices,
        crypto_prices.calendar,
    )
    return (
        kr,
        us,
        crypto,
        {
            "kr_snap": kr_snap,
            "us_snap": us_snap,
            "kr_fund": kr_fund,
            "us_val": us_val,
            "flow": flow,
            "crypto_snap": crypto_snap,
        },
    )


def decision_grid(ctx: S.MarketContext, raw) -> list[dt.date]:
    """Dates where EVERY table a market's sources need is materially complete."""
    if ctx.market == "crypto":
        base = set(P.complete_partition_dates(raw["crypto_snap"], min_rows=100))
        return sorted(base)
    if ctx.market == "kr":
        base = set(P.complete_partition_dates(raw["kr_snap"]))
        base &= set(P.complete_partition_dates(raw["kr_fund"]))
        base &= set(P.complete_partition_dates(raw["flow"], min_rows=100))
    else:
        base = set(P.complete_partition_dates(raw["us_snap"]))
        base &= set(P.complete_partition_dates(raw["us_val"]))
    # a decision date must also be a real market session in the price panel
    sessions = set(ctx.prices.calendar.tolist())
    return sorted(base & sessions)


def run_market(ctx, grid, rsi_lookup, out_rows, gate_cache, universe_rows=None):
    market = ctx.market
    if market == "kr":
        builders = dict(_KR_BUILDERS)
    elif market == "us":
        builders = dict(_US_BUILDERS)
    else:
        builders = dict(_CRYPTO_BUILDERS)

    variants = CRYPTO_GATE_VARIANTS if market == "crypto" else GATE_VARIANTS
    recent_cut = (
        grid[-RECENT_WINDOW_SESSIONS:]
        if market != "crypto"
        else grid[-RECENT_WINDOW_CALENDAR_DAYS:]
    )
    recent_set = set(recent_cut)

    calendar = ctx.prices.calendar
    for day_i, day in enumerate(grid):
        sessions_after = int(np.searchsorted(calendar, day, side="right"))
        sessions_after = int(calendar.size - sessions_after)
        if day_i % 10 == 0:
            _log(f"  {market} {day} ({day_i + 1}/{len(grid)})")
        # ---- sources ---------------------------------------------------
        pools: dict[str, list[str]] = {}
        for sid, fn in builders.items():
            pools[sid] = fn(ctx, day)
        if market in ("kr", "us"):
            pools[f"{market}.tv_rsi45"] = S.src_tv_rsi45(ctx, day, rsi_lookup)
        pools[f"{market}.random"] = S.src_random(ctx, day, TOP_N)
        benchmark_symbols = S.src_benchmark(ctx, day)

        # ---- gate evidence for every pooled symbol ----------------------
        needed = {sym for pool in pools.values() for sym in pool}
        for sym in needed:
            key = (market, sym, day)
            if key in gate_cache:
                continue
            if market == "crypto":
                row = ctx.crypto.get(day)
                rsi = None
                if row is not None and not row.empty:
                    hit = row.loc[row["symbol"] == sym, "rsi"]
                    if len(hit) and pd.notna(hit.iloc[0]):
                        rsi = float(hit.iloc[0])
                gate_cache[key] = S.GateEvidence(rsi, False, 0)
            else:
                gate_cache[key] = S.evaluate_gate(
                    ctx.prices, sym, day, GATE_SUPPORT_WITHIN_PCT
                )

        # ---- gate + top-N + score --------------------------------------
        for sid, pool in pools.items():
            for variant in variants:
                if sid.endswith(".random") and variant not in ("none",):
                    continue
                gated = _apply_gate(market, pool, day, variant, gate_cache)
                selected = gated[:TOP_N]
                for rank, sym in enumerate(selected, start=1):
                    for h in HORIZONS:
                        o = scoring.score(ctx.prices, sym, day, h)
                        out_rows.append(
                            {
                                "market": market,
                                "source_id": sid,
                                "gate": variant,
                                "decision_date": day,
                                "symbol": sym,
                                "rank": rank,
                                "horizon": h,
                                "status": o.status,
                                "entry": o.entry,
                                "ret": o.ret,
                                "mfe": o.mfe,
                                "mae": o.mae,
                                "hl_mfe": o.ret_hl_mfe,
                                "hl_mae": o.ret_hl_mae,
                                "bars_used": o.bars_used,
                                "sessions_after": sessions_after,
                                "censored": bool(sessions_after < h),
                                "window": "recent" if day in recent_set else "earlier",
                                "pool_size": len(pool),
                                "gated_size": len(gated),
                            }
                        )
        # ---- benchmark --------------------------------------------------
        for h in HORIZONS:
            rets = []
            for sym in benchmark_symbols:
                o = scoring.score(ctx.prices, sym, day, h)
                if o.ret is not None:
                    rets.append(o.ret)
                    if universe_rows is not None:
                        universe_rows.append((market, day, h, sym, o.ret, o.status))
            if rets:
                out_rows.append(
                    {
                        "market": market,
                        "source_id": f"{market}.benchmark",
                        "gate": "none",
                        "decision_date": day,
                        "symbol": "__EQW__",
                        "rank": 1,
                        "horizon": h,
                        "status": "full",
                        "entry": None,
                        "ret": float(np.mean(rets)),
                        "mfe": None,
                        "mae": None,
                        "hl_mfe": None,
                        "hl_mae": None,
                        "bars_used": h,
                        "sessions_after": sessions_after,
                        "censored": bool(sessions_after < h),
                        "window": "recent" if day in recent_set else "earlier",
                        "pool_size": len(benchmark_symbols),
                        "gated_size": len(rets),
                    }
                )


def _apply_gate(market, pool, day, variant, gate_cache):
    if variant == "none":
        return list(pool)
    kept = []
    for sym in pool:
        ev = gate_cache.get((market, sym, day))
        if ev is None or ev.rsi is None or ev.rsi > GATE_RSI_MAX:
            continue
        if variant == "rsi45_only":
            kept.append(sym)
        elif variant == "A_strong":
            if ev.has_strong_support_within:
                kept.append(sym)
        elif variant == "B_moderate2":
            if ev.independent_families_within >= GATE_B_MIN_INDEPENDENT_FAMILIES:
                kept.append(sym)
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="research/screener_bakeoff/artifacts")
    ap.add_argument("--markets", default="kr,us,crypto")
    args = ap.parse_args()
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    kr, us, crypto, raw = build_contexts()
    wanted = set(args.markets.split(","))

    _log("precomputing RSI panels ...")
    rsi_kr = _rsi_lookup(kr.prices) if "kr" in wanted else {}
    rsi_us = _rsi_lookup(us.prices) if "us" in wanted else {}
    _log(f"  rsi kr={len(rsi_kr)} us={len(rsi_us)}")

    rows: list[dict] = []
    universe_rows: list[tuple] = []
    gate_cache: dict = {}
    grids = {}
    for ctx, lookup in ((kr, rsi_kr), (us, rsi_us), (crypto, {})):
        if ctx.market not in wanted:
            continue
        grid = decision_grid(ctx, raw)
        grids[ctx.market] = [d.isoformat() for d in grid]
        _log(f"{ctx.market}: {len(grid)} decision dates {grid[0]} .. {grid[-1]}")
        run_market(ctx, grid, lookup, rows, gate_cache, universe_rows)

    df = pd.DataFrame(rows)
    df.to_csv(outdir / "picks.csv", index=False)
    pd.DataFrame(
        universe_rows,
        columns=["market", "decision_date", "horizon", "symbol", "ret", "status"],
    ).to_csv(outdir / "universe_returns.csv", index=False)
    _log(f"wrote {len(df)} pick rows")

    meta = {
        "experiment_id": EXPERIMENT_ID,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "grids": grids,
        "top_n": TOP_N,
        "horizons": list(HORIZONS),
        "sr_window_bars": SR_WINDOW_BARS,
        "gate": {
            "rsi_max": GATE_RSI_MAX,
            "support_within_pct": GATE_SUPPORT_WITHIN_PCT,
            "b_min_independent_families": GATE_B_MIN_INDEPENDENT_FAMILIES,
            "upside_leg": "NEUTRALISED (no point-in-time consensus history)",
        },
        "source_count": len(SOURCES),
    }
    (outdir / "run_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False)
    )
    _log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
