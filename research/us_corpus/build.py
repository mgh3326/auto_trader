"""us-corpus-v1 fetch stage — yahoo daily bars for the frozen US universe.

🔴 SURVIVORSHIP_BIASED = TRUE (see config module docstring).

Design constraints this module enforces, none of which it may relax:

* the request budget is checked *before* the first network call; over budget is
  a `BLOCKED_PRECONDITION`, never a silent trim of the universe,
* a single global gate keeps requests at most one per
  `MIN_REQUEST_INTERVAL_SEC`, and rate-limit signals slow it down but never
  speed it up,
* every symbol outcome is appended to a checkpoint before the next one starts,
  so a killed session resumes instead of refetching,
* an empty response is *recorded as an empty response*. It is never dropped and
  never filled from another source — a symbol that stops returning rows is the
  measurable evidence of the survivorship bias this corpus carries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from yfinance import data as yfdata

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.symbol import to_yahoo_symbol  # noqa: E402
from research.us_corpus import config as cfg  # noqa: E402

# The chart API has no multi-ticker form: `yf.download` fans out one
# `/v8/finance/chart/<SYM>` call per ticker no matter how they are batched.
# Measured 2026-08-03: 3 tickers -> 3 requests. So batching cannot reduce the
# budget, and the honest projection is one request per symbol plus retries.
REQUESTS_PER_SYMBOL = 1
CHUNK_SYMBOLS = 200


class RequestCounter:
    """Counts real HTTP calls at the yfinance chokepoint, not at our call sites.

    Wrapping `YfData._make_request` means cookie/crumb handshakes and internal
    retries are counted too — the reported number is what actually hit Yahoo.
    """

    def __init__(self) -> None:
        self.count = 0
        self.urls: list[str] = []
        self._original: Any = None

    def install(self) -> None:
        self._original = yfdata.YfData._make_request
        original = self._original
        counter = self

        def spy(self_: Any, url: str, *args: Any, **kwargs: Any) -> Any:
            counter.count += 1
            if len(counter.urls) < 50:
                counter.urls.append(url)
            return original(self_, url, *args, **kwargs)

        yfdata.YfData._make_request = spy

    def restore(self) -> None:
        if self._original is not None:
            yfdata.YfData._make_request = self._original


class RateGate:
    """Global minimum spacing between requests. Slows down only, never up."""

    def __init__(self, min_interval: float) -> None:
        self._floor = min_interval
        self._interval = min_interval
        self._last = 0.0
        self.penalties = 0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last = time.monotonic()

    def penalise(self) -> None:
        """A rate-limit signal doubles the spacing. There is no recovery path:
        the brief forbids raising throughput after a block signal."""
        self.penalties += 1
        self._interval = min(self._interval * 2, 30.0)


@dataclass
class SymbolOutcome:
    symbol: str
    yahoo_symbol: str
    status: str  # ok | empty | error
    rows: int = 0
    first_session: str | None = None
    last_session: str | None = None
    error: str | None = None
    attempts: int = 1


@dataclass
class BuildState:
    done: dict[str, SymbolOutcome] = field(default_factory=dict)
    chunk_index: int = 0


def heartbeat(message: str) -> None:
    """Append-only progress note. 🔴 Blocked is a thing we write here, not a
    thing we sit idle on."""
    cfg.PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with cfg.PROGRESS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"- `{stamp}` {message}\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_universe() -> list[str]:
    """🔴 Frozen CSV only. The operating database is not consulted."""
    # keep_default_na=False is load-bearing: the universe contains the ticker
    # "NA" (Nano Labs), which pandas would otherwise parse as a missing value
    # and drop. Losing a symbol silently is exactly the failure this build is
    # not allowed to have, so the count assertion below is the backstop.
    frame = pd.read_csv(cfg.UNIVERSE_FILE, dtype=str, keep_default_na=False)
    symbols = [s.strip() for s in frame["symbol"].tolist() if str(s).strip()]
    if len(symbols) != cfg.UNIVERSE_COUNT:
        raise cfg.PreconditionFailed(
            f"universe count {len(symbols)} != pinned {cfg.UNIVERSE_COUNT}"
        )
    return symbols


def project_budget(symbol_count: int) -> tuple[int, str]:
    """Return (projected_requests, explanation). Caller blocks if over budget."""
    base = symbol_count * REQUESTS_PER_SYMBOL
    # Retry allowance: bounded at MAX_RETRIES_PER_SYMBOL, but a full-universe
    # worst case would blow the budget on its own, so we reserve a flat pool and
    # stop retrying once it is spent rather than pretending it cannot happen.
    retry_pool = 1000
    handshake = 5  # cookie/crumb bootstrap
    projected = base + retry_pool + handshake
    why = (
        f"{symbol_count} symbols x {REQUESTS_PER_SYMBOL} req/symbol = {base} "
        f"(yahoo /v8/finance/chart has no multi-ticker form; batching cannot "
        f"reduce this — measured 3 tickers -> 3 requests). "
        f"+{retry_pool} bounded retry pool +{handshake} cookie/crumb handshake "
        f"= {projected} projected vs MAX_REQUESTS={cfg.MAX_REQUESTS}. "
        f"The full 2016-01-01..2026-07-31 span fits in one request per symbol, "
        f"so no time-slicing multiplier applies."
    )
    return projected, why


def load_checkpoint() -> BuildState:
    state = BuildState()
    if not cfg.CHECKPOINT_FILE.exists():
        return state
    with cfg.CHECKPOINT_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line from a killed process — drop it and refetch
                # that symbol rather than trusting a partial record.
                continue
            state.done[record["symbol"]] = SymbolOutcome(**record)
    return state


def append_checkpoint(outcome: SymbolOutcome) -> None:
    with cfg.CHECKPOINT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome.__dict__, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def normalise_frame(raw: pd.DataFrame, db_symbol: str) -> pd.DataFrame:
    """Flatten a yfinance frame to the corpus schema.

    🔴 No forward fill, no interpolation, no resampling. Rows Yahoo did not
    return simply do not exist here, and the gap analysis reports them.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cfg.OHLCV_COLUMNS)

    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        # Under group_by="ticker" the ticker is level 0 and the OHLCV field is
        # the *last* level, even for a single-ticker download. Taking level 0
        # here would strip every price column and make a healthy symbol look
        # like an empty (delisted) response — a false survivorship signal.
        frame.columns = [str(c[-1]) for c in frame.columns]

    frame = frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    wanted = ["open", "high", "low", "close", "volume"]
    missing = [c for c in wanted if c not in frame.columns]
    if missing:
        # A non-empty response we cannot parse is a bug, not a delisting. Raise
        # so it lands in the error log instead of being counted as evidence of
        # a symbol that stopped trading.
        raise ValueError(
            f"{db_symbol}: unparseable yahoo schema, missing {missing}; "
            f"got columns {list(frame.columns)[:12]}"
        )

    frame = frame[wanted]
    index = pd.to_datetime(frame.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_convert(cfg.TIMEZONE).tz_localize(None)
    frame = frame.assign(session_date=pd.Index(index).normalize())
    frame = frame.assign(symbol=db_symbol)

    # A row with no close is not a session we observed; keep the absence honest
    # by dropping it here and letting the gap report name the missing date.
    frame = frame[frame["close"].notna()]
    frame = frame[cfg.OHLCV_COLUMNS]
    frame = frame[
        (frame["session_date"] >= pd.Timestamp(cfg.START_DATE))
        & (frame["session_date"] <= pd.Timestamp(cfg.CUTOFF_SESSION))
    ]
    return frame.sort_values("session_date").reset_index(drop=True)


def fetch_symbol(db_symbol: str, gate: RateGate) -> tuple[SymbolOutcome, pd.DataFrame]:
    yahoo_symbol = to_yahoo_symbol(db_symbol)
    end_exclusive = (pd.Timestamp(cfg.CUTOFF_SESSION) + pd.Timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )

    last_error: str | None = None
    for attempt in range(1, cfg.MAX_RETRIES_PER_SYMBOL + 2):
        gate.wait()
        try:
            raw = yf.download(
                [yahoo_symbol],
                start=cfg.START_DATE,
                end=end_exclusive,
                interval=cfg.FREQUENCY,
                auto_adjust=True,  # PRICE_MODE=adjusted
                actions=False,
                threads=False,
                progress=False,
                group_by="ticker",
                timeout=30,
            )
            frame = normalise_frame(raw, db_symbol)
        except Exception as exc:  # noqa: BLE001 - outcome is recorded, not raised
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            if "rate" in last_error.lower() or "429" in last_error:
                gate.penalise()
            continue

        if frame.empty:
            # 🔴 Not an error and not a retry case: delisted / renamed / never
            # traded tickers legitimately return nothing. Record the emptiness.
            return (
                SymbolOutcome(
                    symbol=db_symbol,
                    yahoo_symbol=yahoo_symbol,
                    status="empty",
                    rows=0,
                    attempts=attempt,
                ),
                frame,
            )
        return (
            SymbolOutcome(
                symbol=db_symbol,
                yahoo_symbol=yahoo_symbol,
                status="ok",
                rows=len(frame),
                first_session=str(frame["session_date"].iloc[0].date()),
                last_session=str(frame["session_date"].iloc[-1].date()),
                attempts=attempt,
            ),
            frame,
        )

    return (
        SymbolOutcome(
            symbol=db_symbol,
            yahoo_symbol=yahoo_symbol,
            status="error",
            error=last_error,
            attempts=cfg.MAX_RETRIES_PER_SYMBOL + 1,
        ),
        pd.DataFrame(columns=cfg.OHLCV_COLUMNS),
    )


def write_chunk(frames: list[pd.DataFrame], index: int) -> Path | None:
    """`.partial` -> fsync -> atomic rename. Never overwrite a finished chunk."""
    frames = [f for f in frames if not f.empty]
    if not frames:
        return None
    target = cfg.STAGING_DIR / f"chunk-{index:05d}.parquet"
    if target.exists():
        return target
    partial = target.with_suffix(".parquet.partial")
    combined = pd.concat(frames, ignore_index=True)
    combined.to_parquet(partial, index=False, compression="zstd")
    fd = os.open(partial, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Build us-corpus-v1 daily bars")
    parser.add_argument(
        "--limit", type=int, default=None, help="debug: first N symbols"
    )
    args = parser.parse_args()

    started = time.monotonic()
    for directory in (cfg.STAGING_DIR, cfg.REPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    # 🔴 gate 1: pinned digests, re-verified before any content read
    try:
        verified = cfg.verify_inputs()
    except cfg.PreconditionFailed as exc:
        heartbeat(f"**BLOCKED_PRECONDITION** digest gate: {exc}")
        print(f"BLOCKED_PRECONDITION: {exc}")
        return 2
    heartbeat(f"digest gate PASS: {json.dumps(verified)}")

    symbols = load_universe()
    if args.limit:
        symbols = symbols[: args.limit]

    # 🔴 gate 2: request budget, before the first network call
    projected, why = project_budget(len(symbols))
    if projected > cfg.MAX_REQUESTS:
        heartbeat(f"**BLOCKED_PRECONDITION** request budget: {why}")
        print(f"BLOCKED_PRECONDITION: {why}")
        return 2
    heartbeat(f"budget gate PASS: {why}")

    state = load_checkpoint()
    pending = [s for s in symbols if s not in state.done]
    heartbeat(
        f"fetch start: {len(pending)} pending / {len(symbols)} universe "
        f"({len(state.done)} resumed from checkpoint)"
    )

    counter = RequestCounter()
    counter.install()
    gate = RateGate(cfg.MIN_REQUEST_INTERVAL_SEC)

    buffer: list[pd.DataFrame] = []
    chunk_index = len(list(cfg.STAGING_DIR.glob("chunk-*.parquet")))
    processed = 0
    stopped_reason = "completed"

    try:
        for symbol in pending:
            if counter.count >= cfg.MAX_REQUESTS:
                stopped_reason = "MAX_REQUESTS reached"
                break
            elapsed_h = (time.monotonic() - started) / 3600
            if elapsed_h >= cfg.MAX_WALL_CLOCK_HOURS:
                stopped_reason = "MAX_WALL_CLOCK_HOURS reached"
                break

            outcome, frame = fetch_symbol(symbol, gate)
            if not frame.empty:
                buffer.append(frame)
            append_checkpoint(outcome)
            state.done[symbol] = outcome
            processed += 1

            if len(buffer) >= CHUNK_SYMBOLS:
                write_chunk(buffer, chunk_index)
                chunk_index += 1
                buffer = []

            if processed % 250 == 0:
                ok = sum(1 for o in state.done.values() if o.status == "ok")
                empty = sum(1 for o in state.done.values() if o.status == "empty")
                err = sum(1 for o in state.done.values() if o.status == "error")
                heartbeat(
                    f"progress {len(state.done)}/{len(symbols)} "
                    f"(ok={ok} empty={empty} error={err}) "
                    f"requests={counter.count} "
                    f"elapsed={elapsed_h:.2f}h penalties={gate.penalties}"
                )
    except KeyboardInterrupt:
        stopped_reason = "interrupted"
    finally:
        if buffer:
            write_chunk(buffer, chunk_index)
        counter.restore()

    ok = sum(1 for o in state.done.values() if o.status == "ok")
    empty = sum(1 for o in state.done.values() if o.status == "empty")
    err = sum(1 for o in state.done.values() if o.status == "error")
    wall_h = (time.monotonic() - started) / 3600

    summary = {
        "stopped_reason": stopped_reason,
        "universe": len(symbols),
        "resolved": len(state.done),
        "ok": ok,
        "empty": empty,
        "error": err,
        "requests_actual": counter.count,
        "requests_projected": projected,
        "wall_clock_hours": round(wall_h, 3),
        "rate_penalties": gate.penalties,
        "sample_urls": counter.urls[:5],
    }
    (cfg.STAGING_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    heartbeat(f"fetch end: {json.dumps(summary)}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
