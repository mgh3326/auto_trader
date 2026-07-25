"""ROB-1059 H1 remediation (S1: AC6/AC7/AC8 were unenforced dead code) —
integration tests that fail if quote_mode_pipeline's wiring is ever removed
again. Network-0 throughout (fixture archive/REST openers only).
"""

import hashlib
import io
import zipfile
from datetime import date

import pytest
import quote_mode as qm
import quote_mode_pipeline as qmp
import spot_archive_fetch as saf

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"


def _kline_line(ts: int, close: float) -> str:
    return (
        f"{ts},{close},{close},{close},{close},10.0,{ts + 59_999},1000.0,5,4.0,400.0,0"
    )


def _zip_and_checksum(name: str, content: str) -> tuple[bytes, bytes]:
    buf = io.BytesIO()
    # a FIXED date_time is required for the zip's raw bytes (and therefore its
    # SHA-256) to be reproducible ACROSS separate process invocations, not
    # just within one -- zipfile.writestr()'s default ZipInfo embeds the
    # current wall-clock time, which would otherwise make any pinned-hash
    # assertion non-reproducible on a rerun.
    info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, content)
    zb = buf.getvalue()
    checksum = f"{hashlib.sha256(zb).hexdigest()}  {name}\n".encode()
    return zb, checksum


def _monthly_archive_entry(
    symbol: str, year: int, month: int, minute_closes: dict[int, float]
) -> dict[str, bytes]:
    lines = [HEADER]
    for ts in sorted(minute_closes):
        lines.append(_kline_line(ts, minute_closes[ts]) + "\n")
    text = "".join(lines)
    name = f"{symbol}-1m-{year:04d}-{month:02d}.csv"
    zb, chk = _zip_and_checksum(name, text)
    url = saf.spot_kline_monthly_url(symbol, "1m", year, month)
    return {url: zb, url + ".CHECKSUM": chk}


def _monthly_archive_entry_custom_ohlc(
    symbol: str,
    year: int,
    month: int,
    ts: int,
    o: float,
    h: float,
    low: float,
    c: float,
) -> dict[str, bytes]:
    # like _monthly_archive_entry, but lets a single minute's open/high/low/
    # close diverge from each other (needed to reproduce a defect where ONLY
    # one non-close leg's basis division overflows to non-finite).
    line = (
        f"{ts},{o},{h},{low},{c},10.0,{ts + 59_999},1000.0,5,4.0,400.0,0\n"
    )
    text = HEADER + line
    name = f"{symbol}-1m-{year:04d}-{month:02d}.csv"
    zb, chk = _zip_and_checksum(name, text)
    url = saf.spot_kline_monthly_url(symbol, "1m", year, month)
    return {url: zb, url + ".CHECKSUM": chk}


def _fake_sealed(
    base: str,
    quote_mode: str,
    usdc_first: date | None = None,
    usdt_first: date | None = None,
) -> dict[str, qm.SealedPairRecord]:
    return {
        base: qm.SealedPairRecord(
            base=base,
            quote_mode=quote_mode,
            binance_usdc_first_1m=usdc_first,
            binance_usdt_first_1m=usdt_first,
            excluded=False,
            ineligible_reason=None,
        )
    }


def _boom(url: str) -> bytes:
    raise AssertionError(f"must not fetch: {url}")


MONTH_START_MS, _ = saf.month_bounds_ms(2024, 6)
MIN_MS = 60_000
DAY_MS = 86_400_000


# --------------------------------------------------------------------------- #
# AC8: fail closed on a sealed-map mismatch BEFORE any network fetch.
# --------------------------------------------------------------------------- #
def test_ac8_seal_mismatch_fails_closed_before_any_fetch():
    sealed = _fake_sealed("AAA", "USDC", usdc_first=date(2020, 1, 1))
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        qmp.build_quote_mode_aware_corpus(
            base="AAA",
            computed_quote_mode="SYNTH_USDC",  # disagrees with sealed "USDC"
            computed_usdc_first_1m=date(2020, 1, 1),
            computed_usdt_first_1m=date(2019, 1, 1),
            sealed=sealed,
            window_start_ms=MONTH_START_MS,
            window_end_ms=MONTH_START_MS + 5 * MIN_MS,
            archive_opener=_boom,
            rest_opener=_boom,
        )  # _boom proves NO fetch was attempted before the seal check ran


def test_resolve_and_validate_candidate_quote_mode_fails_closed_on_date_drift():
    # sealed AAA/USDC recorded usdc_first_1m=2024-01-01; a fresh recomputation
    # disagreeing on that date must fail closed even though the resulting
    # quote_mode string ("USDC") happens to still match.
    sealed = _fake_sealed("AAA", "USDC", usdc_first=date(2024, 1, 1))
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        qmp.resolve_and_validate_candidate_quote_mode(
            base="AAA",
            base_usdc_first_1m=date(2020, 1, 1),  # disagrees with sealed
            base_usdt_first_1m=None,
            usdc_usdt_available=True,
            required_backtest_start=date(2024, 6, 1),
            sealed=sealed,
        )


def test_resolve_and_validate_candidate_quote_mode_returns_computed_mode_when_it_agrees():
    sealed = _fake_sealed("AAA", "USDC", usdc_first=date(2020, 1, 1))
    mode = qmp.resolve_and_validate_candidate_quote_mode(
        base="AAA",
        base_usdc_first_1m=date(2020, 1, 1),
        base_usdt_first_1m=None,
        usdc_usdt_available=True,
        required_backtest_start=date(2024, 6, 1),
        sealed=sealed,
    )
    assert mode == "USDC"


def test_no_mapping_dispatch_is_never_reachable_and_raises_defensively():
    sealed = _fake_sealed("ZZZ", "NO_MAPPING")
    with pytest.raises(qmp.QuoteModePipelineError):
        qmp.build_quote_mode_aware_corpus(
            base="ZZZ",
            computed_quote_mode="NO_MAPPING",
            computed_usdc_first_1m=None,
            computed_usdt_first_1m=None,
            sealed=sealed,
            window_start_ms=MONTH_START_MS,
            window_end_ms=MONTH_START_MS + 5 * MIN_MS,
            archive_opener=_boom,
            rest_opener=_boom,
        )


# --------------------------------------------------------------------------- #
# AC6: SYNTH_USDC actually synthesizes P = P_USDT / P_USDCUSDT, dropping (not
# forward-filling) any minute where USDCUSDT is missing.
# --------------------------------------------------------------------------- #
def test_synth_usdc_dispatch_synthesizes_price_and_drops_missing_basis_minute():
    base = "SYN"
    sealed = _fake_sealed(
        base, "SYNTH_USDC", usdc_first=date(2024, 9, 1), usdt_first=date(2020, 1, 1)
    )
    window_start = MONTH_START_MS
    window_end = MONTH_START_MS + 5 * MIN_MS

    usdt_closes = {
        window_start + m * MIN_MS: 100.0 + m for m in range(5)
    }  # 100,101,102,103,104
    usdcusdt_closes = {
        window_start + m * MIN_MS: 1.0002
        for m in range(5)
        if m != 2  # minute 2's USDCUSDT basis is deliberately MISSING
    }
    table = {
        **_monthly_archive_entry(f"{base}USDT", 2024, 6, usdt_closes),
        **_monthly_archive_entry("USDCUSDT", 2024, 6, usdcusdt_closes),
    }

    def archive_opener(url: str):
        return table.get(url)

    def rest_opener(url: str):
        return None

    rows, manifest = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="SYNTH_USDC",
        computed_usdc_first_1m=date(2024, 9, 1),
        computed_usdt_first_1m=date(2020, 1, 1),
        sealed=sealed,
        window_start_ms=window_start,
        window_end_ms=window_end,
        archive_opener=archive_opener,
        rest_opener=rest_opener,
    )

    # minute 2 (missing USDCUSDT basis) must be DROPPED, never forward-filled.
    assert [r.open_time_ms for r in rows] == [
        window_start + m * MIN_MS for m in (0, 1, 3, 4)
    ]
    for m, row in zip((0, 1, 3, 4), rows, strict=True):
        expected = qm.synth_usdc_price(100.0 + m, 1.0002)
        assert row.close == pytest.approx(expected)
        assert row.symbol == f"{base}USDC"

    assert manifest.symbol == f"{base}USDC"
    assert manifest.quote_mode == "SYNTH_USDC"
    assert manifest.row_count == 4
    assert manifest.expected_count == 5
    assert manifest.missing_open_times_ms == (window_start + 2 * MIN_MS,)
    # both underlying fetches' provenance must be preserved (AC3) -- a synth
    # corpus is built from TWO real tickers, not fabricated from nothing.
    assert len(manifest.sources) == 2
    assert all(s.checksum_sha256 for s in manifest.sources)


def test_synth_usdc_price_wiring_removed_regression_guard():
    # if AC6 wiring were ever removed (e.g. dispatch just returned the raw
    # USDT rows unchanged instead of dividing by USDCUSDT), this would catch
    # it: the synthesized close must differ from the raw USDT close whenever
    # USDCUSDT is off-peg.
    base = "SYN2"
    sealed = _fake_sealed(
        base, "SYNTH_USDC", usdc_first=date(2024, 9, 1), usdt_first=date(2020, 1, 1)
    )
    window_start = MONTH_START_MS
    window_end = MONTH_START_MS + 1 * MIN_MS
    table = {
        **_monthly_archive_entry(f"{base}USDT", 2024, 6, {window_start: 100.0}),
        **_monthly_archive_entry("USDCUSDT", 2024, 6, {window_start: 1.01}),
    }
    rows, _ = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="SYNTH_USDC",
        computed_usdc_first_1m=date(2024, 9, 1),
        computed_usdt_first_1m=date(2020, 1, 1),
        sealed=sealed,
        window_start_ms=window_start,
        window_end_ms=window_end,
        archive_opener=lambda url: table.get(url),
        rest_opener=lambda url: None,
    )
    assert len(rows) == 1
    assert rows[0].close != 100.0  # NOT the raw USDT close -- actually divided
    assert rows[0].close == pytest.approx(100.0 / 1.01)


def test_synth_usdc_drops_the_whole_minute_when_only_a_non_close_leg_overflows():
    # CodeRabbit A: only `close`'s conversion was ever None-checked; `open`/
    # `high`/`low` were assigned an unnarrowed `qm.synth_usdc_price(...)`
    # result directly. Here `close` (and open/low) divide cleanly but `high`
    # is a corrupted/extreme value whose OWN division overflows to a
    # non-finite quotient -- the pre-fix code would silently construct a
    # NormalizedKline with `high=None` in a `float` field instead of dropping
    # the minute (AC6: never forward-fill/partially-fabricate a minute).
    base = "OVF"
    sealed = _fake_sealed(
        base, "SYNTH_USDC", usdc_first=date(2024, 9, 1), usdt_first=date(2020, 1, 1)
    )
    window_start = MONTH_START_MS
    window_end = MONTH_START_MS + 1 * MIN_MS
    huge_high = 1.79e308  # near float max: huge_high / 0.5 overflows to inf
    table = {
        **_monthly_archive_entry_custom_ohlc(
            f"{base}USDT", 2024, 6, window_start, 100.0, huge_high, 100.0, 100.0
        ),
        **_monthly_archive_entry("USDCUSDT", 2024, 6, {window_start: 0.5}),
    }
    rows, manifest = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="SYNTH_USDC",
        computed_usdc_first_1m=date(2024, 9, 1),
        computed_usdt_first_1m=date(2020, 1, 1),
        sealed=sealed,
        window_start_ms=window_start,
        window_end_ms=window_end,
        archive_opener=lambda url: table.get(url),
        rest_opener=lambda url: None,
    )
    # the minute must be DROPPED entirely -- never a row with `high=None`.
    assert rows == []
    assert manifest.row_count == 0
    assert manifest.expected_count == 1
    assert manifest.missing_open_times_ms == (window_start,)


def test_synth_usdc_converts_quote_denominated_volumes_by_the_same_basis():
    # CodeRabbit B: `quote_volume`/`taker_buy_quote_volume` come from the
    # source `{base}USDT` row and are USDT-denominated; a `SYNTH_USDC` row
    # must divide them by the SAME per-minute basis as the OHLC legs, or the
    # corpus silently mixes USDT notionals into a series labeled SYNTH_USDC.
    # `base_volume`/`taker_buy_volume` (base-denominated) and `trade_count`
    # must stay untouched.
    base = "VOL"
    sealed = _fake_sealed(
        base, "SYNTH_USDC", usdc_first=date(2024, 9, 1), usdt_first=date(2020, 1, 1)
    )
    window_start = MONTH_START_MS
    window_end = MONTH_START_MS + 1 * MIN_MS
    basis = 1.0002
    table = {
        **_monthly_archive_entry(f"{base}USDT", 2024, 6, {window_start: 100.0}),
        **_monthly_archive_entry("USDCUSDT", 2024, 6, {window_start: basis}),
    }
    rows, _ = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="SYNTH_USDC",
        computed_usdc_first_1m=date(2024, 9, 1),
        computed_usdt_first_1m=date(2020, 1, 1),
        sealed=sealed,
        window_start_ms=window_start,
        window_end_ms=window_end,
        archive_opener=lambda url: table.get(url),
        rest_opener=lambda url: None,
    )
    assert len(rows) == 1
    row = rows[0]
    # base-denominated fields pass through UNCONVERTED.
    assert row.base_volume == 10.0
    assert row.taker_buy_volume == 4.0
    assert row.trade_count == 5
    # quote-denominated (USDT) notionals ARE converted by the same basis as
    # the OHLC legs -- a regression to raw passthrough would leave these at
    # the source USDT values (1000.0 / 400.0) instead.
    assert row.quote_volume == pytest.approx(1000.0 / basis)
    assert row.taker_buy_quote_volume == pytest.approx(400.0 / basis)
    assert row.quote_volume != 1000.0
    assert row.taker_buy_quote_volume != 400.0


# --------------------------------------------------------------------------- #
# AC7: USDT_PROXY dispatch records the per-UTC-day basis-drift flag onto the
# manifest (recorded only, never applied/excluded).
# --------------------------------------------------------------------------- #
def test_usdt_proxy_dispatch_records_per_day_basis_drift_flags():
    base = "PXY"
    sealed = _fake_sealed(base, "USDT_PROXY", usdt_first=date(2019, 1, 1))
    day0 = MONTH_START_MS
    day1 = MONTH_START_MS + DAY_MS
    window_start = day0
    window_end = day0 + 2 * DAY_MS

    table = {
        **_monthly_archive_entry(f"{base}USDT", 2024, 6, {day0: 100.0, day1: 110.0}),
        **_monthly_archive_entry(
            "USDCUSDT", 2024, 6, {day0: 1.0000, day1: 1.0050}
        ),  # day0 at peg, day1 50bp off (>30bp threshold)
    }

    rows, manifest = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="USDT_PROXY",
        computed_usdc_first_1m=None,
        computed_usdt_first_1m=date(2019, 1, 1),
        sealed=sealed,
        window_start_ms=window_start,
        window_end_ms=window_end,
        archive_opener=lambda url: table.get(url),
        rest_opener=lambda url: None,
    )

    assert manifest.symbol == f"{base}USDT"
    assert manifest.quote_mode == "USDT_PROXY"
    # USDT_PROXY dispatch must NOT drop/alter rows based on USDCUSDT presence
    # (unlike SYNTH_USDC) -- the flag is recorded, never applied.
    assert [r.open_time_ms for r in rows] == [day0, day1]
    assert [r.close for r in rows] == [100.0, 110.0]
    assert manifest.usdcusdt_basis_drift_flags == (
        ("2024-06-01", False),
        ("2024-06-02", True),
    )


def test_usdcusdt_basis_drift_flag_wiring_removed_regression_guard():
    # if AC7 wiring were ever removed, usdcusdt_basis_drift_flags would stay
    # at its default empty tuple even for a USDT_PROXY symbol -- this must
    # never be empty when the window actually spans real days.
    base = "PXY2"
    sealed = _fake_sealed(base, "USDT_PROXY", usdt_first=date(2019, 1, 1))
    day0 = MONTH_START_MS
    table = {
        **_monthly_archive_entry(f"{base}USDT", 2024, 6, {day0: 50.0}),
        **_monthly_archive_entry("USDCUSDT", 2024, 6, {day0: 1.0}),
    }
    _, manifest = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="USDT_PROXY",
        computed_usdc_first_1m=None,
        computed_usdt_first_1m=date(2019, 1, 1),
        sealed=sealed,
        window_start_ms=day0,
        window_end_ms=day0 + DAY_MS,
        archive_opener=lambda url: table.get(url),
        rest_opener=lambda url: None,
    )
    assert manifest.usdcusdt_basis_drift_flags != ()
    assert manifest.usdcusdt_basis_drift_flags == (("2024-06-01", False),)


# --------------------------------------------------------------------------- #
# USDC dispatch (baseline passthrough case).
# --------------------------------------------------------------------------- #
def test_usdc_dispatch_builds_directly():
    base = "PLN"
    sealed = _fake_sealed(base, "USDC", usdc_first=date(2018, 1, 1))
    table = _monthly_archive_entry(
        f"{base}USDC", 2024, 6, {MONTH_START_MS: 42.0, MONTH_START_MS + MIN_MS: 43.0}
    )
    rows, manifest = qmp.build_quote_mode_aware_corpus(
        base=base,
        computed_quote_mode="USDC",
        computed_usdc_first_1m=date(2018, 1, 1),
        computed_usdt_first_1m=None,
        sealed=sealed,
        window_start_ms=MONTH_START_MS,
        window_end_ms=MONTH_START_MS + 2 * MIN_MS,
        archive_opener=lambda url: table.get(url),
        rest_opener=lambda url: None,
    )
    assert [r.close for r in rows] == [42.0, 43.0]
    assert manifest.symbol == f"{base}USDC"
    assert manifest.quote_mode == "USDC"
    assert manifest.usdcusdt_basis_drift_flags == ()


# --------------------------------------------------------------------------- #
# real-world wiring: the actual sealed universe_map fixture, not a fake.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def real_sealed():
    path = qmp.__file__.rsplit("/", 1)[0] + "/sealed/universe_map_2026-07-25.json"
    return qm.load_sealed_universe_map(path)


def test_resolve_and_validate_against_the_real_sealed_map_btc(real_sealed):
    mode = qmp.resolve_and_validate_candidate_quote_mode(
        base="BTC",
        base_usdc_first_1m=date(2018, 12, 15),
        base_usdt_first_1m=date(2017, 8, 17),
        usdc_usdt_available=True,
        required_backtest_start=date(2024, 6, 1),
        sealed=real_sealed,
    )
    assert mode == "USDC"


def test_resolve_and_validate_against_the_real_sealed_map_hype_never_revived(
    real_sealed,
):
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        qmp.resolve_and_validate_candidate_quote_mode(
            base="HYPE",
            base_usdc_first_1m=None,
            base_usdt_first_1m=date(2026, 1, 1),  # attempted revival
            usdc_usdt_available=True,
            required_backtest_start=date(2024, 6, 1),
            sealed=real_sealed,
        )


# --------------------------------------------------------------------------- #
# AC3: build_corpus_manifest_with_quote_modes is a REAL, reproducible
# CorpusManifest producer.
# --------------------------------------------------------------------------- #
def test_build_corpus_manifest_with_quote_modes_is_reproducible_on_rerun():
    base_usdc = "PLN"
    base_proxy = "PXY"
    sealed = {
        **_fake_sealed(base_usdc, "USDC", usdc_first=date(2018, 1, 1)),
        **_fake_sealed(base_proxy, "USDT_PROXY", usdt_first=date(2019, 1, 1)),
    }
    table = {
        **_monthly_archive_entry(f"{base_usdc}USDC", 2024, 6, {MONTH_START_MS: 42.0}),
        **_monthly_archive_entry(f"{base_proxy}USDT", 2024, 6, {MONTH_START_MS: 7.0}),
        **_monthly_archive_entry("USDCUSDT", 2024, 6, {MONTH_START_MS: 1.0}),
    }
    candidates = [
        qmp.CandidateQuoteModeSpec(base_usdc, "USDC", date(2018, 1, 1), None),
        qmp.CandidateQuoteModeSpec(base_proxy, "USDT_PROXY", None, date(2019, 1, 1)),
    ]

    def run():
        return qmp.build_corpus_manifest_with_quote_modes(
            candidates,
            sealed=sealed,
            window_start_ms=MONTH_START_MS,
            window_end_ms=MONTH_START_MS + MIN_MS,
            archive_opener=lambda url: table.get(url),
            rest_opener=lambda url: None,
        )

    rows1, manifest1 = run()
    rows2, manifest2 = run()

    assert manifest1.content_hash() == manifest2.content_hash()
    assert manifest1.to_dict() == manifest2.to_dict()
    # canonical lexicographic order by FINAL symbol: "PLNUSDC" < "PXYUSDT"
    assert manifest1.symbols == (f"{base_usdc}USDC", f"{base_proxy}USDT")
    assert rows1[f"{base_usdc}USDC"][0].close == 42.0
    assert rows1[f"{base_proxy}USDT"][0].close == 7.0
    # pinned, reproducible-on-demand digest for this exact fixture (computed
    # by running this exact test body) -- a silent change to canonical
    # hashing, field set, or dispatch logic changes this value.
    assert (
        manifest1.content_hash()
        == "1d04f3b62e221f442942d8ff7ff3d9c6fc00957ba77e5f282a57831ea3f24ac5"
    )
