"""ROB-351 (eng-review Issue 2) — point-in-time universe manifest.

Single survivorship authority consulted by BOTH the cost-blind screen and the
gate. Mandatory leak guards: a symbol unlisted as-of a timestamp, or already
delisted, must NOT appear in that timestamp's tradeable universe. Cross-sectional
strategies query the universe as-of EACH rebalance (Codex hardening), not once
per window.

ts/listed_from/delisted_at/min_seasoning are all integers in the SAME unit
(caller-defined, e.g. epoch ms); delisted_at is exclusive, None = still live.
"""

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
