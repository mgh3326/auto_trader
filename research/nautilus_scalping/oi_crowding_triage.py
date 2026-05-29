#!/usr/bin/env python3
"""ROB-362 PR3 — OI-crowding GROSS-triage (crypto strategy book-closer).

Pre-registered, ONE-shot, cost-blind measurement: does the OI-crowding STATE
(``oi_zscore`` extreme = crowded positioning) carry ANY *gross* (fee-free) edge in
forward price returns? This is the only proposal that survived the 25-agent strategy
postmortem (fork B = close the crypto book). It is NOT a strategy: no params are tuned,
no execution model is built. If the gross expectancy clears the 0.5bps triviality floor
out-of-sample we escalate to a bounded backtest; otherwise we write the auditable
negative and close family-4 / the crypto strategy line.

Skeptical prior (pre-registered): the nearest neighbour ROB-342 (funding/liquidation
cascade FADE) was net-negative even at 0bps — "dead on gross edge". The crowding STATE
feature here is distinct from that cascade SHOCK, but the prior is skeptical, not hopeful.

Method (reuses the ROB-353 seam — no new backtest engine):
  * Signal is resampled to ONE reading per UTC day (the day's last ``oi_zscore``), so the
    5-min OI grid is aligned to the 1d price grid — this avoids counting ~288 correlated
    intra-day "trades" off a single daily close (the over-counting trap).
  * Entry when ``|oi_zscore| >= THRESHOLD``; both directions are measured as SEPARATE
    pre-registered hypotheses: ``fade`` (trade against the crowd) and ``ride`` (with it).
  * Forward return is close-to-close over HORIZON_DAYS; entries are NON-OVERLAPPING
    (no re-entry while a position is held) so samples are not autocorrelated by overlap.
  * gross_expectancy / oos_gross are built by ``campaign_specs._summary_from_trades`` and
    judged by the existing cost-blind screen (``discovery.screen.classify``, 0.5bps floor,
    OOS split frozen at 2025-01-01).

Read-only PUBLIC data only (price klines + the ROB-356 OI feature CSVs). Operator-gated
behind ``--run``; no orders, no Demo, no live/mainnet endpoint, no scheduler, no DB,
no raw market data committed.

Usage (operator):
    cd research/nautilus_scalping
    export AUTO_TRADER_RESEARCH_ARTIFACT_ROOT=/Users/mgh3326/shared/auto_trader_research_artifacts
    uv run --no-project python oi_crowding_triage.py --run --out
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import campaign_specs as cs
import families
from discovery.screen import classify

_DAY_MS = 86_400_000

# --------------------------------------------------------------------------- #
# Pre-registered config (FROZEN before the OOS read; recorded as a hash)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TriageConfig:
    """Frozen ex-ante parameters. No tuning: one threshold, one horizon, both signs."""

    oi_zscore_threshold: float = 2.0
    horizon_days: int = 5
    min_samples: int = 200
    economic_triviality_floor_bps: float = 0.5
    oos_split_ts: int = cs.OOS_SPLIT_TS  # 2025-01-01T00:00:00Z
    ref_fee_bps: float = families.REF_FEE_BPS
    notional: float = cs.NOTIONAL
    directions: tuple[str, ...] = ("fade", "ride")
    skeptical_prior: str = (
        "ROB-342 funding/liquidation cascade fade was net-negative even at 0bps "
        "(dead on gross edge); prior is skeptical, not hopeful"
    )

    def to_dict(self) -> dict:
        return {
            "oi_zscore_threshold": self.oi_zscore_threshold,
            "horizon_days": self.horizon_days,
            "min_samples": self.min_samples,
            "economic_triviality_floor_bps": self.economic_triviality_floor_bps,
            "oos_split_ts": self.oos_split_ts,
            "ref_fee_bps": self.ref_fee_bps,
            "notional": self.notional,
            "directions": list(self.directions),
            "skeptical_prior": self.skeptical_prior,
        }

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode()
        ).hexdigest()


FROZEN = TriageConfig()


# --------------------------------------------------------------------------- #
# Pure signal/return machinery (unit-tested; no network)
# --------------------------------------------------------------------------- #
def _close_asof(series: Sequence[tuple[int, float]], ts: int) -> float | None:
    """Most recent close at or before ``ts`` (mirrors ``panel._close_at_or_before``;
    kept local to avoid importing a private name)."""
    found: float | None = None
    for s_ts, close in series:
        if s_ts > ts:
            break
        found = close
    return found


def _utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()


def daily_oi_zscore(feats: Sequence[dict]) -> list[tuple[int, float]]:
    """Resample the 5-min OI feature rows to ONE ``(ts, oi_zscore)`` per UTC day: the
    day's LAST non-null ``oi_zscore`` (the end-of-day crowding reading). Rows must be
    chronological. Days with no usable z are dropped. This aligns the dense OI grid to
    the 1d price grid so a single daily close is not traded ~288 times."""
    by_day: dict[str, tuple[int, float]] = {}
    for r in feats:
        z = r.get("oi_zscore")
        ts = r.get("ts")
        if z is None or ts is None:
            continue
        by_day[_utc_day(int(ts))] = (int(ts), float(z))  # last write wins -> day's last
    return [by_day[d] for d in sorted(by_day)]


def crowding_trades(
    daily_signal: Sequence[tuple[int, float]],
    closes: Sequence[tuple[int, float]],
    *,
    direction: str,
    threshold: float,
    horizon_days: int,
    notional: float,
    ref_fee_bps: float,
) -> list[families.Trade]:
    """Non-overlapping close-to-close trades from a daily crowding signal.

    direction ``fade`` trades AGAINST the crowd (position = -sign(z)); ``ride`` trades
    WITH it (position = +sign(z)). A trade opens only when ``|z| >= threshold`` and no
    prior trade is still held; it requires a genuine forward bar (full horizon realized)."""
    if direction not in ("fade", "ride"):
        raise ValueError(f"direction must be 'fade' or 'ride', got {direction!r}")
    if not closes:
        return []
    sign_mult = -1.0 if direction == "fade" else 1.0
    last_close_ts = closes[-1][0]
    horizon_ms = horizon_days * _DAY_MS
    trades: list[families.Trade] = []
    next_free_ts = -1
    for ts, z in daily_signal:
        if ts < next_free_ts or abs(z) < threshold:
            continue
        forward_ts = ts + horizon_ms
        if last_close_ts < forward_ts:  # full horizon not realizable -> drop
            continue
        entry = _close_asof(closes, ts)
        fwd = _close_asof(closes, forward_ts)
        if entry is None or fwd is None or entry == 0.0:
            continue
        raw_ret = (fwd - entry) / entry
        position = sign_mult * (1.0 if z > 0 else -1.0)
        gross_pnl = position * raw_ret * notional
        trades.append(families.make_taker_trade(gross_pnl, ts, notional, ref_fee_bps))
        next_free_ts = forward_ts  # non-overlapping hold
    return trades


def build_specs(
    oi_features_by_symbol: dict[str, Sequence[dict]],
    closes_by_symbol: dict[str, Sequence[tuple[int, float]]],
    cfg: TriageConfig = FROZEN,
) -> list[dict]:
    """One spec per direction: pool non-overlapping trades across symbols, summarize."""
    specs: list[dict] = []
    for direction in cfg.directions:
        pooled: list[families.Trade] = []
        for symbol in sorted(oi_features_by_symbol):
            closes = closes_by_symbol.get(symbol)
            if not closes:
                continue
            daily = daily_oi_zscore(oi_features_by_symbol[symbol])
            pooled.extend(
                crowding_trades(
                    daily,
                    closes,
                    direction=direction,
                    threshold=cfg.oi_zscore_threshold,
                    horizon_days=cfg.horizon_days,
                    notional=cfg.notional,
                    ref_fee_bps=cfg.ref_fee_bps,
                )
            )
        pooled.sort(key=lambda t: t.ts_opened)
        name = f"oi_crowding_{direction}"
        specs.append(
            {
                "name": name,
                "summary": cs._summary_from_trades(name, pooled, cfg.oos_split_ts),
                "direction": direction,
            }
        )
    return specs


def triage(specs: Sequence[dict], cfg: TriageConfig = FROZEN) -> list[dict]:
    """Cost-blind classify each direction; the union verdict closes the book unless ANY
    direction clears the gross floor AND holds out of sample."""
    out: list[dict] = []
    for spec in specs:
        c = classify(
            spec["summary"],
            cost_blind=True,
            min_samples=cfg.min_samples,
            min_gross_bps=cfg.economic_triviality_floor_bps,
        )
        out.append({"direction": spec["direction"], "classified": c})
    return out


def overall_verdict(triaged: Sequence[dict]) -> str:
    """``edge_found`` iff some direction promoted (gross > floor AND OOS gross > 0);
    ``needs_more_data`` if any is sample-starved and none promoted; else ``screened_out``
    (the expected book-closing negative)."""
    recs = {t["classified"].recommendation for t in triaged}
    if "promote_to_full_validation" in recs:
        return "edge_found"
    if "needs_more_data" in recs:
        return "needs_more_data"
    return "screened_out"


def _direction_row(direction: str, c) -> dict:
    """Flatten one direction's classified result to the fields the verdict reports."""
    return {
        "direction": direction,
        "recommendation": c.recommendation,
        "reason": c.reason,
        "sample_count": c.summary.sample_count,
        "gross_expectancy_bps": c.summary.gross_expectancy_bps,
        "oos_gross_bps": c.summary.oos_gross_bps,
        "fee_adjusted_bps": c.summary.fee_adjusted_bps,
        "in_sample_only": c.in_sample_only,
        "cost_binding": c.cost_binding,
    }


# --------------------------------------------------------------------------- #
# Feature CSV load (pure)
# --------------------------------------------------------------------------- #
def load_oi_features(path) -> list[dict]:
    """Read a ROB-356 per-symbol feature CSV into ``{ts:int, oi_zscore:float|None}`` rows
    (only the fields the triage needs), chronological."""
    rows: list[dict] = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            z = r.get("oi_zscore")
            rows.append(
                {
                    "ts": int(r["ts"]),
                    "oi_zscore": (float(z) if z not in (None, "") else None),
                }
            )
    rows.sort(key=lambda x: x["ts"])
    return rows


# --------------------------------------------------------------------------- #
# Operator RUN (network: price klines + on-disk OI feature CSVs)
# --------------------------------------------------------------------------- #
def _summary_payload(triaged: Sequence[dict], cfg: TriageConfig) -> dict:
    return {
        "schema_version": "oi_crowding_triage.v1",
        "config_hash": cfg.config_hash(),
        "config": cfg.to_dict(),
        "overall_verdict": overall_verdict(triaged),
        "directions": [
            _direction_row(t["direction"], t["classified"]) for t in triaged
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ROB-362 PR3 OI-crowding gross-triage (read-only, operator-gated)."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Perform the RUN (loads price + OI features).",
    )
    parser.add_argument(
        "--manifest",
        default="data_manifests/pit_universe.v1.json",
        help="PIT universe manifest path.",
    )
    parser.add_argument(
        "--coverage",
        default=None,
        help="Path to funding_oi_coverage.v1.json (default: artifact-root discovery/rob356).",
    )
    parser.add_argument(
        "--interval", default="1d", help="Price kline interval (default 1d)."
    )
    parser.add_argument(
        "--out",
        action="store_true",
        help="Write the triage verdict JSON to the gitignored artifact root.",
    )
    args = parser.parse_args(argv)

    if not args.run:
        print(
            "DRY: would gross-triage the OI-crowding signal (oi_zscore both directions) "
            f"on the dense+survivorship-ok subset.\nFrozen config hash: {FROZEN.config_hash()}\n"
            "Add --run to load price + OI features and print the verdict; --out to persist."
        )
        return 0

    import pit_bars
    import pit_universe as pu
    from artifact_paths import resolve_artifact_path

    cov_path = (
        Path(args.coverage)
        if args.coverage
        else resolve_artifact_path("discovery", "rob356", "funding_oi_coverage.v1.json")
    )
    coverage = json.loads(Path(cov_path).read_text())
    subset = sorted(
        s["symbol"]
        for s in coverage["per_symbol"]
        if s.get("missingness", 1.0) <= 0.05 and s.get("survivorship_ok")
    )
    print(f"dense+survivorship-ok subset: {len(subset)} symbols")
    print(f"frozen config hash: {FROZEN.config_hash()}")

    manifest = pu.PITManifest.load(args.manifest).strict_usdt_perp()
    feat_dir = resolve_artifact_path("discovery", "rob356", "features")

    # Ensure price klines on disk (operator network); then load PIT-trimmed closes.
    _ensure_klines(subset, manifest, args.interval)
    closes_by_symbol = pit_bars.load_panel(subset, args.interval, manifest)
    oi_features_by_symbol: dict[str, list[dict]] = {}
    for sym in subset:
        csv_path = feat_dir / f"{sym}.csv"
        if csv_path.exists():
            oi_features_by_symbol[sym] = load_oi_features(csv_path)

    specs = build_specs(oi_features_by_symbol, closes_by_symbol, FROZEN)
    triaged = triage(specs, FROZEN)
    payload = _summary_payload(triaged, FROZEN)

    print(f"\noverall_verdict: {payload['overall_verdict']}")
    for d in payload["directions"]:
        oos = d["oos_gross_bps"]
        oos_s = f"{oos:.3f}" if oos is not None else "None"
        print(
            f"  [{d['direction']:4}] {d['recommendation']:26} "
            f"gross={d['gross_expectancy_bps']:.3f}bps oos_gross={oos_s} "
            f"n={d['sample_count']}"
        )
        print(f"          {d['reason']}")

    if args.out:
        out = resolve_artifact_path("discovery", "rob362", "oi_crowding_triage.v1.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))
        print(f"\nverdict written (gitignored): {out}")
    else:
        print("\n(no --out: nothing written to disk)")
    return 0


def _ensure_klines(symbols: Sequence[str], manifest, interval: str) -> None:
    """Fetch monthly price klines for any subset symbol not yet on disk (public archive)."""
    import pit_klines_fetcher as kf
    from artifact_paths import pit_data_root

    root = pit_data_root()
    listings = {x.symbol: x for x in manifest.listings}
    for sym in symbols:
        sym_dir = Path(root) / "klines" / interval / sym
        if sym_dir.is_dir() and any(sym_dir.iterdir()):
            continue
        listing = listings.get(sym)
        if listing is None:
            continue
        from_month = _month(listing.listed_from)
        to_month = _month(listing.delisted_at) if listing.delisted_at else _now_month()
        print(f"  fetch klines {sym} {interval} {from_month}..{to_month}")
        try:
            kf.fetch_months(sym, interval, from_month, to_month, out_root=root)
        except Exception as exc:  # noqa: BLE001 — a missing month must not abort the panel
            print(f"    klines fetch partial for {sym}: {type(exc).__name__}")


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


def _now_month() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m")


if __name__ == "__main__":
    sys.exit(main())
