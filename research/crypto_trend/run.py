"""ROB-3796 exploration CLI: archive download and the three-L backtest.

Research only: public unauthenticated GETs of the USD-M kline archive, local
files, no app, database, broker or scheduler import. Artifacts live in ``--root``
(outside git); the repository report carries aggregates only.

    python -m research.crypto_trend.run fetch    --root DIR
    python -m research.crypto_trend.run backtest --root DIR
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .archive import Bar, fetch_month, month_list, zip_name
from .http import make_urllib_fetch, with_retries
from .metrics import max_drawdown, net_return, sharpe
from .signal import BAR_MS, DAY_MS
from .simulate import (
    CAP_USD,
    GROSS_CAPITAL_USD,
    Configuration,
    SymbolData,
    run_btc_long_only,
    run_candidate,
    run_cash,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
INTERVALS = ("4h", "1d")
LOOKBACKS_DAYS = (20, 60, 120)  # the only tuned parameter; three configurations
# Archive months used: June 2023 (warm-up for L=120 and the EWMA window)
# through October 2026 (the most recent complete month).
DATA_FROM = "2023-06"
DATA_TO_EXCL = "2026-11"
# Exploration window: the most recent three years of the archive.
WINDOW_START = datetime(2023, 11, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 1, tzinfo=UTC)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def cmd_fetch(root: Path) -> int:
    fetch = with_retries(make_urllib_fetch())
    cache = root / "cache"
    rows = []
    for symbol in SYMBOLS:
        for interval in INTERVALS:
            for month in month_list(DATA_FROM, DATA_TO_EXCL):
                got = fetch_month(fetch, cache, symbol, interval, month)
                rows.append(
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "month": month,
                        "file": zip_name(symbol, interval, month),
                        "zip_sha256": got.zip_sha256,
                        "bars": len(got.bars),
                        "from_cache": got.from_cache,
                    }
                )
                print(f"{symbol} {interval} {month} bars={len(got.bars)}", flush=True)
    (root / "manifest.json").write_text(json.dumps(rows, indent=1) + "\n")
    print(f"wrote {len(rows)} verified month files")
    return 0


def _load_bars(cache: Path, symbol: str, interval: str) -> list[Bar]:
    bars: list[Bar] = []
    for month in month_list(DATA_FROM, DATA_TO_EXCL):
        bars.extend(fetch_month(None, cache, symbol, interval, month).bars)
    ordered = sorted(bars, key=lambda b: b.open_ms)
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if cur.open_ms == prev.open_ms:
            raise ValueError(f"duplicate bar {symbol} {interval} {cur.open_ms}")
    return ordered


def load_market(root: Path) -> dict[str, SymbolData]:
    cache = root / "cache"
    market: dict[str, SymbolData] = {}
    for symbol in SYMBOLS:
        h4 = _load_bars(cache, symbol, "4h")
        d1 = _load_bars(cache, symbol, "1d")
        market[symbol] = SymbolData(
            closes_4h={b.open_ms: float(b.close) for b in h4},
            opens_4h={b.open_ms: float(b.open) for b in h4},
            daily_closes={b.open_ms: float(b.close) for b in d1},
        )
    return market


def coverage(market: dict[str, SymbolData]) -> dict[str, dict[str, object]]:
    """Per symbol and interval: first/last bar and missing bars on the grid."""
    start = _ms(datetime(2023, 6, 1, tzinfo=UTC))
    end = _ms(datetime(2026, 11, 1, tzinfo=UTC))
    window_lo, window_hi = _ms(WINDOW_START), _ms(WINDOW_END)
    out: dict[str, dict[str, object]] = {}
    for symbol in SYMBOLS:
        data = market[symbol]
        for interval, present, step in (
            ("4h", data.closes_4h, BAR_MS),
            ("1d", data.daily_closes, DAY_MS),
        ):
            keys = sorted(present)
            grid = range(start, end, step)
            missing = [t for t in grid if t not in present]
            in_window = [
                t for t in range(window_lo, window_hi, step) if t not in present
            ]
            out[f"{symbol} {interval}"] = {
                "first_open_utc": _iso(keys[0]),
                "last_open_utc": _iso(keys[-1]),
                "grid_points_data_range": len(grid),
                "present": len(present),
                "missing_data_range": len(missing),
                "missing_in_window": len(in_window),
                "window_points": len(range(window_lo, window_hi, step)),
            }
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def _metrics(cfg: Configuration) -> dict[str, object]:
    eps = cfg.episodes
    long_eps = [e for e in eps if e.direction > 0]
    short_eps = [e for e in eps if e.direction < 0]
    per_symbol: dict[str, float] = {}
    for ep in eps:
        per_symbol[ep.symbol] = per_symbol.get(ep.symbol, 0.0) + ep.pnl_usd
    sleeves = cfg.sleeves
    requests = sum(s.entry_requests for s in sleeves)
    filled = sum(s.entries_filled for s in sleeves)
    decisions = sum(s.decisions for s in sleeves)
    no_signal = sum(s.no_signal for s in sleeves)
    return {
        "net_return": net_return(cfg.base_usd, cfg.equity),
        "net_usd": cfg.equity[-1] - cfg.base_usd,
        "max_drawdown": max_drawdown(cfg.base_usd, cfg.equity),
        "sharpe": sharpe(cfg.base_usd, cfg.equity),
        "trades": len(eps),
        "long_trades": len(long_eps),
        "short_trades": len(short_eps),
        "open_at_window_end": sum(1 for e in eps if e.exit_reason == "window_end_open"),
        "max_hold_exits": sum(1 for e in eps if e.exit_reason == "max_hold"),
        "entry_requests": requests,
        "entries_filled": filled,
        "signal_to_entry_rate": (filled / requests) if requests else None,
        "decision_points": decisions,
        "no_signal_points": no_signal,
        "missing_grid_bars": sum(s.missing_grid_bars for s in sleeves),
        "max_gross_usd": cfg.max_gross_usd,
        "max_concurrent": cfg.max_concurrent,
        "pnl_by_symbol_usd": per_symbol,
        "long_pnl_usd": sum(e.pnl_usd for e in long_eps),
        "short_pnl_usd": sum(e.pnl_usd for e in short_eps),
    }


def run_all(market: dict[str, SymbolData]) -> dict[str, object]:
    window_lo, window_hi = _ms(WINDOW_START), _ms(WINDOW_END)
    cash = run_cash(window_lo, window_hi)
    results: dict[str, object] = {
        "window": {"start_utc": _iso(window_lo), "end_exclusive_utc": _iso(window_hi)},
        "cap_usd": CAP_USD,
        "gross_capital_usd": GROSS_CAPITAL_USD,
        "by_lookback": {},
    }
    by_l: dict[str, object] = {}
    for lookback in LOOKBACKS_DAYS:
        cand = run_candidate(market, SYMBOLS, lookback, window_lo, window_hi)
        btc = run_btc_long_only(market, lookback, window_lo, window_hi)
        by_l[str(lookback)] = {
            "candidate-tsmom": _metrics(cand),
            "control-btc-long-only": _metrics(btc),
            "control-cash": _metrics(cash),
        }
    results["by_lookback"] = by_l
    return results


def choose_lookback(results: dict[str, object]) -> int:
    """Pre-stated rule: highest candidate net return; ties go to the shorter L."""
    by_l = results["by_lookback"]
    assert isinstance(by_l, dict)
    scored = [
        (-float(by_l[str(L)]["candidate-tsmom"]["net_return"]), L)  # type: ignore[index]
        for L in LOOKBACKS_DAYS
    ]
    return min(scored)[1]


def _fmt_pct(value: object) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.2f}%"


def _fmt_num(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def tables_markdown(results: dict[str, object]) -> str:
    by_l = results["by_lookback"]
    assert isinstance(by_l, dict)
    lines = [
        "| L (days) | configuration | net return | MDD | Sharpe | trades (long/short) |"
        " open at end | signal-to-entry (filled/requested) | no-signal / decision points |"
        " missing grid bars |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for L in LOOKBACKS_DAYS:
        block = by_l[str(L)]
        for name in ("candidate-tsmom", "control-btc-long-only", "control-cash"):
            m = block[name]
            rate = m["signal_to_entry_rate"]
            rate_txt = (
                "n/a"
                if rate is None
                else f"{m['entries_filled']}/{m['entry_requests']} ({float(rate) * 100:.1f}%)"
            )
            lines.append(
                f"| {L} | {name} | {_fmt_pct(m['net_return'])} | "
                f"{_fmt_pct(m['max_drawdown'])} | {_fmt_num(m['sharpe'])} | "
                f"{m['trades']} ({m['long_trades']}/{m['short_trades']}) | "
                f"{m['open_at_window_end']} | {rate_txt} | "
                f"{m['no_signal_points']} / {m['decision_points']} | "
                f"{m['missing_grid_bars']} |"
            )
    return "\n".join(lines) + "\n"


def cmd_backtest(root: Path) -> int:
    market = load_market(root)
    cov = coverage(market)
    results = run_all(market)
    chosen = choose_lookback(results)
    results["chosen_lookback_days"] = chosen
    results["coverage"] = cov
    (root / "results.json").write_text(
        json.dumps(results, indent=1, default=str) + "\n"
    )
    (root / "tables.md").write_text(tables_markdown(results))
    print(tables_markdown(results))
    print(f"chosen L = {chosen}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "backtest"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    if args.command == "fetch":
        return cmd_fetch(root)
    return cmd_backtest(root)


if __name__ == "__main__":
    raise SystemExit(main())
