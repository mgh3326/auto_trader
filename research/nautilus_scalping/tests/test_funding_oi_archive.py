"""ROB-356 (PR1) — pure parsers for Binance USD-M funding + OI (metrics) archives.

No network here: the parsers take the decompressed CSV text (the network/zip read
is the operator-gated builder's job). These tests pin the confirmed archive schemas:

    fundingRate (monthly): calc_time,funding_interval_hours,last_funding_rate
        calc_time is epoch MS UTC; realized last_funding_rate is known only at/after
        calc_time; funding_interval_hours is per-row (8h->4h changes live in the data).
    metrics (daily): create_time,symbol,sum_open_interest,sum_open_interest_value,
        count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
        count_long_short_ratio,sum_taker_long_short_vol_ratio
        create_time is a STRING UTC datetime at 5-min grid; duplicate rows occur and
        must be deduped by (symbol, create_time).
"""

from datetime import UTC, datetime

import funding_oi_archive as foa


def _ms(s: str) -> int:
    return int(
        datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1000
    )


# --------------------------------------------------------------------------- #
# funding
# --------------------------------------------------------------------------- #
FUNDING_CSV = (
    "calc_time,funding_interval_hours,last_funding_rate\n"
    "1577836800000,8,-0.00012359\n"
    "1577865600000,8,-0.00012383\n"
)


def test_parse_funding_csv_basic():
    rows = foa.parse_funding_csv(FUNDING_CSV)
    assert len(rows) == 2
    r = rows[0]
    assert r.calc_time == 1577836800000  # epoch ms UTC, kept as-is
    assert r.funding_interval_hours == 8
    assert r.last_funding_rate == -0.00012359


def test_parse_funding_csv_skips_header():
    # header row must never be parsed as data
    rows = foa.parse_funding_csv(FUNDING_CSV)
    assert all(isinstance(r.calc_time, int) for r in rows)
    assert len(rows) == 2


def test_parse_funding_csv_preserves_per_row_interval_change():
    # 8h -> 4h interval change must survive as a per-row feature, not a constant
    csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1700000000000,8,0.0001\n"
        "1700028800000,4,0.0002\n"  # interval dropped to 4h
        "1700043200000,4,0.00015\n"
    )
    rows = foa.parse_funding_csv(csv)
    assert [r.funding_interval_hours for r in rows] == [8, 4, 4]


def test_parse_funding_csv_sorted_by_calc_time():
    csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1700028800000,8,0.0002\n"
        "1700000000000,8,0.0001\n"  # out of order
    )
    rows = foa.parse_funding_csv(csv)
    assert [r.calc_time for r in rows] == [1700000000000, 1700028800000]


# --------------------------------------------------------------------------- #
# metrics / open interest
# --------------------------------------------------------------------------- #
METRICS_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
)
METRICS_CSV = METRICS_HEADER + (
    "2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,"
    "1.17547937,1.23012681,1.35731217,0.78373373\n"
    "2020-09-01 00:05:00,BTCUSDT,39100.00000000,456400000.00000000,"
    "1.10000000,1.20000000,1.30000000,0.80000000\n"
)


def test_parse_metrics_csv_basic():
    rows = foa.parse_metrics_csv(METRICS_CSV)
    assert len(rows) == 2
    r = rows[0]
    assert r.create_time == _ms("2020-09-01 00:00:00")  # string parsed as UTC epoch ms
    assert r.symbol == "BTCUSDT"
    assert r.sum_open_interest == 39080.231
    assert r.sum_open_interest_value == 456144339.23360443
    assert r.count_toptrader_long_short_ratio == 1.17547937
    assert r.sum_taker_long_short_vol_ratio == 0.78373373


def test_parse_metrics_csv_dedupes_identical_timestamp_rows():
    # observed in the real archive: same (symbol, create_time) row repeated
    dup = (
        METRICS_HEADER
        + (
            "2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,"
            "1.17547937,1.23012681,1.35731217,0.78373373\n"
        )
        * 2
    )
    rows = foa.parse_metrics_csv(dup)
    assert len(rows) == 1


def test_parse_metrics_csv_sorted_by_create_time():
    csv = METRICS_HEADER + (
        "2020-09-01 00:05:00,BTCUSDT,2,2,1,1,1,1\n"
        "2020-09-01 00:00:00,BTCUSDT,1,1,1,1,1,1\n"  # out of order
    )
    rows = foa.parse_metrics_csv(csv)
    assert [r.create_time for r in rows] == [
        _ms("2020-09-01 00:00:00"),
        _ms("2020-09-01 00:05:00"),
    ]


def test_parse_metrics_csv_empty_ratio_field_is_none():
    # some symbols/days have blank ratio columns; OI must still parse, ratio -> None
    csv = METRICS_HEADER + "2020-09-01 00:00:00,BTCUSDT,39080.231,456144339.23,,,,\n"
    (r,) = foa.parse_metrics_csv(csv)
    assert r.sum_open_interest == 39080.231
    assert r.count_toptrader_long_short_ratio is None
    assert r.sum_taker_long_short_vol_ratio is None


def test_parse_metrics_csv_quoted_empty_ratio_field_is_none():
    # REAL archives (ROB-360 finding) encode empty ratio columns as CSV-quoted empty
    # fields ("") rather than bare blanks. A naive split(",") leaves the literal 2-char
    # string '""' and float('""') raises -> the whole symbol was being SKIP'd, which
    # disproportionately killed 2022-era delisted symbols (ROB-349 survivorship). The
    # parser must treat a quoted-empty cell as None and still parse populated columns.
    csv = METRICS_HEADER + (
        '2022-01-26 03:35:00,1000BTTCUSDT,2023567.00000000,4176.01498223,"","",8.5,""\n'
    )
    (r,) = foa.parse_metrics_csv(csv)
    assert r.symbol == "1000BTTCUSDT"
    assert r.sum_open_interest == 2023567.0
    assert r.sum_open_interest_value == 4176.01498223
    assert r.count_toptrader_long_short_ratio is None
    assert r.sum_toptrader_long_short_ratio is None
    assert r.count_long_short_ratio == 8.5
    assert r.sum_taker_long_short_vol_ratio is None
