"""ROB-1059 H1 (AC23) — fake-free smoke test.

Real synthetic persistence -> the real offline loader -> the real daily-bar
builder -> the real PIT universe builder, chained end to end. The ONLY fake
things in this test are (a) the injectable network openers feeding
``corpus_builder.build_symbol_corpus`` (an in-memory fixture table — the
network-0 discipline this whole issue requires) and (b) the raw synthetic
minute-bar content. Every processing stage after that fixture boundary is the
REAL, named module function — no stage is replaced by a mock/stub/behavior
callback anywhere in this test.
"""

import hashlib
import io
import zipfile

import canonical_hash
import corpus_builder as cb
import daily_bars as db
import persistence as p
import pit_universe_alpaca as pu
import spot_archive_fetch as saf

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
DAY_MS = 86_400_000
MIN_MS = 60_000


def _kline_line(ts: int, close: float) -> str:
    high = close + 1.0
    low = close - 1.0
    return f"{ts},{close},{high},{low},{close},10.0,{ts + 59_999},1000.0,5,4.0,400.0,0"


def _zip_and_checksum(name: str, content: str) -> tuple[bytes, bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    zb = buf.getvalue()
    checksum = f"{hashlib.sha256(zb).hexdigest()}  {name}\n".encode()
    return zb, checksum


def _full_two_day_month_archive_fixture(symbol: str, year: int, month: int):
    """A fake monthly archive covering exactly 2 full UTC days (2880 minutes),
    with the SECOND day's minute 1400 dropped so the daily-bar layer must
    observe: day0 valid, day1 invalid (missing-in-final-60-minutes rule)."""
    month_start_ms, _ = saf.month_bounds_ms(year, month)
    lines = [HEADER]
    day0_start = month_start_ms
    day1_start = month_start_ms + DAY_MS
    for m in range(1440):
        lines.append(
            _kline_line(day0_start + m * MIN_MS, close=100.0 + m * 0.0001) + "\n"
        )
    for m in range(1440):
        if m == 1400:  # inside the final-60-minute window (1380..1439) -> invalid day
            continue
        lines.append(
            _kline_line(day1_start + m * MIN_MS, close=200.0 + m * 0.0001) + "\n"
        )
    text = "".join(lines)
    name = f"{symbol}-1m-{year:04d}-{month:02d}.csv"
    zb, chk = _zip_and_checksum(name, text)
    url = saf.spot_kline_monthly_url(symbol, "1m", year, month)
    table = {url: zb, url + ".CHECKSUM": chk}
    return table, month_start_ms


def test_fake_free_end_to_end_chain_persistence_loader_daily_builder_pit_universe(
    tmp_path,
):
    symbol = "BTCUSDC"
    table, month_start_ms = _full_two_day_month_archive_fixture(symbol, 2024, 6)

    def archive_opener(url: str):
        return table.get(url)

    def rest_opener(url: str):  # never used: this fixture has full archive coverage
        return None

    window_start = month_start_ms
    window_end = month_start_ms + 2 * DAY_MS

    # Stage 1: REAL corpus_builder (network-0 via injected fixture openers)
    rows, manifest = cb.build_symbol_corpus(
        symbol,
        "USDC",
        window_start,
        window_end,
        archive_opener=archive_opener,
        rest_opener=rest_opener,
    )
    assert manifest.sources[0].source == "archive_monthly"
    assert manifest.row_count == 2879  # 2880 - 1 dropped minute
    assert len(manifest.missing_open_times_ms) == 1

    # Stage 2: REAL persistence (write then offline-verify load)
    rel_path, file_sha256 = p.write_symbol_shard(tmp_path, symbol, rows)
    content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    loaded_rows = p.load_symbol_shard(
        tmp_path,
        rel_path,
        expected_file_sha256=file_sha256,
        expected_content_sha256=content_sha256,
        expected_row_count=len(rows),
    )
    assert loaded_rows == rows

    # Stage 3: REAL daily_bars builder, fed from the offline-loaded rows
    spot_minutes = [
        db.SpotMinute(r.open_time_ms, r.open, r.high, r.low, r.close, r.base_volume)
        for r in loaded_rows
    ]
    daily_series = db.build_daily_series(
        spot_minutes, window_start_ms=window_start, window_end_ms=window_end
    )
    assert len(daily_series) == 2
    day0, day1 = daily_series
    assert day0.is_valid is True
    assert day1.is_valid is False  # the dropped minute sits in the final 60 minutes

    # Stage 4: REAL PIT universe builder, driven by the real daily-bar validity
    decision_ts_ms = window_end + 5 * MIN_MS  # 00:05 UTC after the window closes
    candidate = pu.SymbolCandidate(
        symbol=f"{symbol[:-4]}/USD",
        base=symbol[:-4],
        alpaca_active=True,
        alpaca_tradable=True,
        is_usd_pair=True,
        binance_quote_mode="USDC",
        alpaca_first_daily_ms=window_start - 400 * DAY_MS,  # ample warm-up
        all_valid_daily_bars_in_lookback=all(bar.is_valid for bar in daily_series),
        no_gap_in_last_60min=daily_series[-1].gap_in_last_60min is False,
    )
    snapshot = pu.evaluate_universe(decision_ts_ms, [candidate])
    # day1's invalidity must propagate all the way to universe ineligibility --
    # this is the real end-to-end contract, not a stubbed assertion.
    assert snapshot.per_symbol[0].eligible is False
    assert snapshot.per_symbol[0].fail_reason == "invalid_daily_bar_in_lookback"
