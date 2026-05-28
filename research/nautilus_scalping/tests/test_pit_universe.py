"""ROB-351 (eng-review Issue 2) — point-in-time universe manifest.

Single survivorship authority consulted by BOTH the cost-blind screen and the
gate. Mandatory leak guards: a symbol unlisted as-of a timestamp, or already
delisted, must NOT appear in that timestamp's tradeable universe. Cross-sectional
strategies query the universe as-of EACH rebalance (Codex hardening), not once
per window.

ts/listed_from/delisted_at/min_seasoning are all integers in the SAME unit
(caller-defined, e.g. epoch ms); delisted_at is exclusive, None = still live.
"""

import pit_universe
from pit_universe import PITManifest, SymbolListing


def _manifest():
    return PITManifest.from_records([
        {"symbol": "AAA", "listed_from": 100, "delisted_at": None},
        {"symbol": "BBB", "listed_from": 150, "delisted_at": 400},
        {"symbol": "CCC", "listed_from": 500, "delisted_at": None},
    ])


def test_includes_listed_and_not_delisted():
    assert "AAA" in _manifest().universe_as_of(200)


def test_excludes_symbol_unlisted_as_of_ts():
    # LEAK GUARD: CCC lists at 500; must not appear at ts=200
    assert "CCC" not in _manifest().universe_as_of(200)


def test_excludes_symbol_already_delisted():
    # LEAK GUARD: BBB delists at 400 (exclusive); excluded at ts=400 and after
    assert "BBB" not in _manifest().universe_as_of(400)
    assert "BBB" not in _manifest().universe_as_of(450)


def test_includes_symbol_still_tradeable_before_delist():
    assert "BBB" in _manifest().universe_as_of(399)


def test_seasoning_excludes_freshly_listed():
    # AAA listed at 100; with min_seasoning=50 it is eligible only from ts>=150
    assert "AAA" not in _manifest().universe_as_of(120, min_seasoning=50)
    assert "AAA" in _manifest().universe_as_of(150, min_seasoning=50)


def test_as_of_each_rebalance_drops_delisted_symbol():
    m = _manifest()
    rebalances = [200, 350, 450]
    universes = {ts: m.universe_as_of(ts) for ts in rebalances}
    assert "BBB" in universes[200]
    assert "BBB" in universes[350]
    assert "BBB" not in universes[450]  # delisted at 400, gone by next rebalance


def test_round_trip_dict():
    m = _manifest()
    m2 = PITManifest.from_records(m.to_records())
    assert m2.universe_as_of(200) == m.universe_as_of(200)


def test_rejects_delist_before_list():
    try:
        SymbolListing(symbol="X", listed_from=200, delisted_at=100).validate()
    except ValueError:
        return
    raise AssertionError("expected ValueError for delisted_at < listed_from")


def test_symbollisting_optional_metadata_roundtrips():
    rec = {
        "symbol": "EOSUSDT", "listed_from": 1672531200000, "delisted_at": 1700000000000,
        "status": "dead", "kline_coverage": 1.0, "funding_coverage": 1.0,
        "confidence": "high", "missing_data_reason": "delisted",
    }
    m = pit_universe.PITManifest.from_records([rec])
    (only,) = m.listings
    assert only.status == "dead"
    assert only.kline_coverage == 1.0
    assert only.confidence == "high"
    back = pit_universe.PITManifest.from_records(m.to_records())
    assert back.listings[0].missing_data_reason == "delisted"


def test_symbollisting_metadata_defaults_none():
    m = pit_universe.PITManifest.from_records(
        [{"symbol": "BTCUSDT", "listed_from": 0, "delisted_at": None}]
    )
    only = m.listings[0]
    assert only.status is None and only.confidence is None
    assert pit_universe.PITManifest.from_records(m.to_records()).listings[0].symbol == "BTCUSDT"


def test_from_pit_index_records_maps_dates_to_epoch_ms():
    rows = [
        {"symbol": "EOSUSDT", "status": "dead", "first_seen": "2023-01", "last_seen": "2024-01",
         "active_from": "2023-01-26", "active_to": "2024-01-11",
         "kline_coverage": 1.0, "funding_coverage": 1.0, "confidence": "high",
         "missing_data_reason": "delisted"},
        {"symbol": "BTCUSDT", "status": "live", "first_seen": "2020-01", "last_seen": "2026-05",
         "active_from": "2020-01-01", "active_to": "ongoing",
         "kline_coverage": 1.0, "funding_coverage": 1.0, "confidence": "high",
         "missing_data_reason": ""},
    ]
    m = pit_universe.PITManifest.from_pit_index_records(rows)
    eos = next(x for x in m.listings if x.symbol == "EOSUSDT")
    btc = next(x for x in m.listings if x.symbol == "BTCUSDT")
    assert eos.listed_from == pit_universe._date_to_epoch_ms("2023-01-26")
    assert eos.delisted_at == pit_universe._date_to_epoch_ms("2024-01-12")
    assert btc.delisted_at is None
    assert eos.tradeable_at(pit_universe._date_to_epoch_ms("2024-01-11"))
    assert not eos.tradeable_at(pit_universe._date_to_epoch_ms("2024-01-12"))


def test_from_pit_index_records_skips_rows_without_dates():
    rows = [{"symbol": "GHOSTUSDT", "status": "dead", "first_seen": None, "last_seen": None,
             "active_from": None, "active_to": None}]
    m = pit_universe.PITManifest.from_pit_index_records(rows)
    assert m.listings == ()
