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
    REWORK_ID,
    SOURCES,
    SR_WINDOW_BARS,
    TOP_N,
    WITHDRAWN_SOURCES,
    is_withdrawn_source,
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
}
_WITHDRAWN_BUILDERS_BY_MARKET = {
    "crypto": S.src_crypto_rsi45,
}


def _reject_withdrawn_builder_aliases(builders: dict) -> None:
    withdrawn_builders = tuple(_WITHDRAWN_BUILDERS_BY_MARKET.values())
    aliases = {
        source_id
        for source_id, builder in builders.items()
        if builder in withdrawn_builders and source_id not in WITHDRAWN_SOURCES
    }
    if aliases:
        raise ValueError(
            "withdrawn builders must use their canonical source IDs: "
            + ", ".join(sorted(aliases))
        )


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _group_by_date(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    # NOT dict(groupby): pandas GroupBy is not a (key, value) iterable dict() accepts.
    return {d: g for d, g in df.groupby("snapshot_date")}  # noqa: C416


def _builders_for_market(
    market: str,
    wanted_sources: set[str] | None,
    *,
    include_withdrawn_sources: bool = False,
) -> dict:
    if market == "kr":
        builders = dict(_KR_BUILDERS)
    elif market == "us":
        builders = dict(_US_BUILDERS)
    else:
        builders = dict(_CRYPTO_BUILDERS)
    if include_withdrawn_sources:
        builders.update(
            {
                source_id: _WITHDRAWN_BUILDERS_BY_MARKET[market]
                for source_id in WITHDRAWN_SOURCES
                if source_id.startswith(f"{market}.")
            }
        )
    if wanted_sources is not None:
        builders = {
            source_id: builder
            for source_id, builder in builders.items()
            if source_id in wanted_sources
        }
    _reject_withdrawn_builder_aliases(builders)
    return builders


def _validate_requested_sources(
    wanted_sources: set[str] | None, *, include_withdrawn_sources: bool
) -> None:
    requested_withdrawn = (wanted_sources or set()) & WITHDRAWN_SOURCES
    if requested_withdrawn and not include_withdrawn_sources:
        raise ValueError(
            "withdrawn sources require --include-withdrawn-sources: "
            + ", ".join(sorted(requested_withdrawn))
        )


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


def run_market(
    ctx,
    grid,
    out_rows,
    gate_cache,
    universe_rows=None,
    wanted_sources: set[str] | None = None,
    *,
    include_withdrawn_sources: bool = False,
):
    market = ctx.market
    builders = _builders_for_market(
        market,
        wanted_sources,
        include_withdrawn_sources=include_withdrawn_sources,
    )

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
        rnd_sid = f"{market}.random"
        if wanted_sources is None or rnd_sid in wanted_sources:
            pools[rnd_sid] = S.src_random(ctx, day, TOP_N)
        run_benchmark = (
            wanted_sources is None or f"{market}.benchmark" in wanted_sources
        )
        benchmark_symbols = S.src_benchmark(ctx, day) if run_benchmark else []

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
        if not run_benchmark:
            continue
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
    ap.add_argument(
        "--sources",
        default="",
        help="comma-separated source_ids to (re)run; empty = all",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="replace matching source_id rows in existing picks.csv; leave other sources untouched",
    )
    ap.add_argument(
        "--include-withdrawn-sources",
        action="store_true",
        help="explicitly opt in to the withdrawn crypto builder for diagnostics",
    )
    args = ap.parse_args()
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    wanted_sources = (
        {s.strip() for s in args.sources.split(",") if s.strip()}
        if args.sources
        else None
    )
    _validate_requested_sources(
        wanted_sources,
        include_withdrawn_sources=args.include_withdrawn_sources,
    )

    kr, us, crypto, raw = build_contexts()
    wanted = set(args.markets.split(","))

    rows: list[dict] = []
    universe_rows: list[tuple] = []
    gate_cache: dict = {}
    grids = {}
    for ctx in (kr, us, crypto):
        if ctx.market not in wanted:
            continue
        grid = decision_grid(ctx, raw)
        grids[ctx.market] = [d.isoformat() for d in grid]
        _log(f"{ctx.market}: {len(grid)} decision dates {grid[0]} .. {grid[-1]}")
        run_market(
            ctx,
            grid,
            rows,
            gate_cache,
            universe_rows,
            wanted_sources,
            include_withdrawn_sources=args.include_withdrawn_sources,
        )

    df = pd.DataFrame(rows)
    picks_path = outdir / "picks.csv"
    if args.merge and picks_path.exists() and not df.empty:
        old = pd.read_csv(picks_path)
        replaced = set(df["source_id"].unique())
        keep = old[~old["source_id"].isin(replaced)]
        df["decision_date"] = pd.to_datetime(df["decision_date"]).dt.strftime(
            "%Y-%m-%d"
        )
        keep["decision_date"] = pd.to_datetime(keep["decision_date"]).dt.strftime(
            "%Y-%m-%d"
        )
        df = pd.concat([keep, df], ignore_index=True)
        _log(f"merged {sorted(replaced)} into existing picks ({len(keep)} rows kept)")
    df.to_csv(picks_path, index=False)
    if not args.merge:
        pd.DataFrame(
            universe_rows,
            columns=["market", "decision_date", "horizon", "symbol", "ret", "status"],
        ).to_csv(outdir / "universe_returns.csv", index=False)
    _log(f"wrote {len(df)} pick rows")

    meta = {
        "experiment_id": EXPERIMENT_ID,
        "rework_id": REWORK_ID,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "rerun_sources": sorted(wanted_sources) if wanted_sources else "all",
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
        "include_withdrawn_sources": args.include_withdrawn_sources,
        "withdrawn_sources": sorted(WITHDRAWN_SOURCES),
        "tv_rsi45_comparison_withdrawn": True,
        "tv_rsi45_withdrawal_reason": (
            "live top-10 vs research top-100; parity test expected 100 not 10"
        ),
        "source_count": sum(
            not is_withdrawn_source(source.source_id) for source in SOURCES
        ),
    }
    (outdir / "run_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False)
    )
    _log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
