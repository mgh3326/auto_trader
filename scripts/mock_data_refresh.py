"""Publish broad daily inputs for the manual ROB-1474 paper runners."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from research.stock_selection.dataset import decision_at, sessions, validate_bars
from research.stock_selection.io import digest, read_bars
from scripts.mock_data_sources import AlpacaSource, BinanceSource, JsonGet


def completed_days(market: str, now: datetime, lookback: int) -> list[date]:
    days = sessions(
        market,
        (now.date() - timedelta(days=lookback * 2)).isoformat(),
        now.date().isoformat(),
    )
    return [
        d.date() for d in days if decision_at(market, d.date()) <= pd.Timestamp(now)
    ][-lookback:]


def _csv(frame: pd.DataFrame) -> bytes:
    return (
        frame.sort_values(["symbol", "date"])
        .to_csv(index=False, lineterminator="\n")
        .encode()
    )


def _json(payload: dict) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def _write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def refresh(
    root: Path,
    source,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    lookback: int | None = None,
    top_n: int = 10,
    backfill: bool = False,
    dry_run: bool = False,
) -> dict:
    use_wall_clock = now is None
    now = now or datetime.now(UTC)
    market = source.market
    if now.tzinfo is None or market not in {"us", "crypto"}:
        raise ValueError("timezone-aware observation and us/crypto required")
    now = now.astimezone(UTC)
    lookback = lookback if lookback is not None else (280 if market == "us" else 400)
    limit = limit if limit is not None else (1000 if market == "us" else 150)
    minimum = 260 if market == "us" else 365
    if lookback < minimum or top_n < 1 or limit < 2 * top_n:
        raise ValueError("insufficient lookback or universe size")
    config = {
        "market": market,
        "feed": source.feed,
        "limit": limit,
        "lookback": lookback,
        "top_n": top_n,
    }
    days = completed_days(market, now, lookback)
    if len(days) != lookback:
        raise ValueError("calendar lookback unavailable")
    cutoff, observed_date = days[-1].isoformat(), now.date().isoformat()
    if dry_run:
        return {
            **config,
            "dry_run": True,
            "data_cutoff": cutoff,
            "observation_date": observed_date,
        }
    root.mkdir(parents=True, exist_ok=True)
    with (root / f".{market}-refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = root / market
        if current.exists() and not current.is_symlink():
            raise ValueError(
                "producer requires a fresh root; legacy market directories cannot be replaced"
            )
        previous = current.resolve() if current.exists() else None
        old = pd.DataFrame()
        observations = {}
        meta: dict = {}
        if previous:
            _, meta = read_bars(root, market)
            if meta["producer_config"] != config:
                raise ValueError("producer configuration changed; use a fresh root")
            observations = json.loads((previous / "membership.json").read_text())
            if digest(previous / "membership.json") != meta["membership_sha256"]:
                raise ValueError("membership hash mismatch")
            if observed_date < max(observations) or cutoff < meta["data_cutoff"]:
                raise ValueError("refresh cannot backdate observations")
            if observed_date in observations:
                return meta
            if digest(previous / "candidates.csv") != meta["candidates_sha256"]:
                raise ValueError("candidate history hash mismatch")
            old = pd.read_csv(
                previous / "candidates.csv",
                dtype={"symbol": str},
                float_precision="round_trip",
            )
        assets = source.assets()
        if not assets or len(assets) != len(set(assets)):
            raise ValueError("empty or duplicate source universe")
        symbols = sorted(set(assets) | {source.benchmark})
        start = days[0]
        if not old.empty and not backfill:
            # Seven-calendar-day overlap detects recent adjustment/revision changes. Re-fetch any
            # affected symbol's full window rather than mixing price vintages.
            overlap = date.fromisoformat(meta["data_cutoff"]) - timedelta(days=7)
            start = max(start, overlap)
        fresh = source.bars(symbols, start, days[-1])
        present = set(fresh.symbol)
        if not old.empty and not backfill:
            repair = present - set(old.symbol)
            common = old.merge(fresh, on=["symbol", "date"], suffixes=("_old", "_new"))
            for col in ("open", "high", "low", "close", "volume", "traded_value"):
                changed = common[f"{col}_old"].astype(float) != common[
                    f"{col}_new"
                ].astype(float)
                repair.update(common.loc[changed, "symbol"])
            if repair:
                repaired = source.bars(sorted(repair), days[0], days[-1])
                if set(repaired.symbol) != repair:
                    raise ValueError("incomplete adjustment repair")
                fresh = pd.concat(
                    [fresh[~fresh.symbol.isin(repair)], repaired], ignore_index=True
                )
            old = old[~old.symbol.isin(repair)]
            merged = pd.concat([old, fresh], ignore_index=True).drop_duplicates(
                ["symbol", "date"], keep="last"
            )
        else:
            merged = fresh
        merged = merged[
            (merged.date >= days[0].isoformat()) & (merged.date <= cutoff)
        ].copy()
        validated = validate_bars(merged, market)
        if (validated.available_at > pd.Timestamp(now)).any():
            raise ValueError("forming bar refused")
        # Restore stable string serialization after validation.
        validated["date"] = validated.date.dt.strftime("%Y-%m-%d")
        validated["available_at"] = validated.available_at.map(lambda t: t.isoformat())
        merged = validated
        expected = {d.isoformat() for d in days}
        scores, excluded = [], {}
        window = 20 if market == "us" else 30
        for symbol in symbols:
            history = merged[merged.symbol == symbol].sort_values("date")
            if set(history.date) != expected:
                excluded[symbol] = "incomplete_feature_history"
                continue
            if float(history.iloc[-1].volume) <= 0:
                excluded[symbol] = "no_current_volume"
                continue
            value = history.tail(window).traded_value
            score = float(value.median() if market == "us" else value.sum())
            if symbol in assets:
                scores.append((symbol, score))
        selected = [
            symbol for symbol, _ in sorted(scores, key=lambda x: (-x[1], x[0]))[:limit]
        ]
        if source.benchmark in excluded or len(selected) < 2 * top_n:
            raise ValueError(
                "insufficient complete symbols or benchmark; previous snapshot retained"
            )
        if use_wall_clock:
            finished = datetime.now(UTC)
            if finished.date() != now.date():
                raise ValueError(
                    "refresh crossed UTC date; retry without backdating membership"
                )
            now = finished
        observation = {
            "observed_at": now.isoformat(),
            "data_cutoff": cutoff,
            "symbols": selected,
            "ranking": "20_session_median_close_times_volume"
            if market == "us"
            else "30_day_quote_volume_sum",
            "scores": {s: v for s, v in scores if s in selected},
            "excluded": excluded,
        }
        observations[observed_date] = observation
        # Only current members are selectable. Earlier generations remain intact;
        # historical bars here are feature backfills, never past membership claims.
        bars = merged[merged.symbol.isin(selected)].copy()
        benchmark = merged[merged.symbol == source.benchmark].copy()
        benchmark["symbol"] = "__benchmark__"
        bars = pd.concat([bars, benchmark], ignore_index=True)
        content = {
            "bars.csv": _csv(bars),
            "candidates.csv": _csv(merged),
            "membership.json": _json(observations),
            "source_symbols.json": _json(
                {"observed_at": now.isoformat(), "symbols": assets}
            ),
        }
        manifest = {
            "schema": "stock-selection-bars/v1",
            "producer": "mock-data-refresh/v1",
            "market": market,
            "producer_config": config,
            "data_cutoff": cutoff,
            "fetched_at": now.isoformat(),
            "observation_date": observed_date,
            "rows": len(bars),
            "symbols": len(selected),
            "candidate_symbols": len(assets),
            "candidate_rows": len(merged),
            "membership_dates": len(observations),
            "benchmark": source.benchmark,
            "sha256": hashlib.sha256(content["bars.csv"]).hexdigest(),
            "candidates_sha256": hashlib.sha256(content["candidates.csv"]).hexdigest(),
            "membership_sha256": hashlib.sha256(content["membership.json"]).hexdigest(),
            "source_symbols_sha256": hashlib.sha256(
                content["source_symbols.json"]
            ).hexdigest(),
            "adjustment": "all" if market == "us" else "none",
            "universe": "prospective observed membership only; backfilled bars are not historical membership",
            "availability": "nominal completed-session availability; first backfill is current vintage",
            "excluded": excluded,
        }
        content["bars.json"] = _json(manifest)
        generations = root / f".{market}-snapshots"
        generations.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=generations))
        link = root / f".{market}-next"
        try:
            for name, data in content.items():
                _write(stage / name, data)
            if digest(stage / "bars.csv") != manifest["sha256"]:
                raise ValueError("staged bars hash mismatch")
            generation = generations / hashlib.sha256(content["bars.json"]).hexdigest()
            fd = os.open(stage, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(stage, generation)
            fd = os.open(generations, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            link.unlink(missing_ok=True)
            link.symlink_to(generation.relative_to(root), target_is_directory=True)
            os.replace(link, current)
            fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            link.unlink(missing_ok=True)
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("us", "crypto"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="offline plan; no HTTP or writes"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--lookback", type=int)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--feed",
        choices=("iex", "sip"),
        default=os.environ.get("MOCK_DATA_ALPACA_FEED", "iex"),
    )
    args = parser.parse_args(argv)
    try:
        with httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            if args.market == "crypto":
                source = BinanceSource(JsonGet(client))
            elif args.dry_run:
                # No credential lookup for an offline plan.
                source = type("UsPlan", (), {"market": "us", "feed": args.feed})()
            else:
                source = AlpacaSource(JsonGet(client), args.feed)
            result = refresh(
                args.root,
                source,
                limit=args.limit,
                lookback=args.lookback,
                top_n=args.top_n,
                backfill=args.backfill,
                dry_run=args.dry_run,
            )
    except Exception as exc:  # noqa: BLE001 - transport errors must not expose credentials
        print(json.dumps({"success": False, "error_class": type(exc).__name__}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
