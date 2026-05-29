# tests/test_pit_bars.py
import pit_bars
import pit_universe

KCOLS = "open_time,open,high,low,close,volume,close_time,qv,n,tbv,tbqv,ig"
DAY = 86_400_000


def _write_csv(root, symbol, interval, month, rows):
    d = root / "klines" / interval / symbol
    d.mkdir(parents=True, exist_ok=True)
    lines = [KCOLS] + [",".join(str(x) for x in r) for r in rows]
    (d / f"{symbol}-{interval}-{month}.csv").write_text("\n".join(lines) + "\n")


def test_load_bars_trims_to_membership_and_zero_vol_tail(tmp_path):
    rows = [
        [0 * DAY, 10, 11, 9, 10, 0.0, 0, 0, 0, 0, 0, 0],
        [1 * DAY, 10, 12, 9, 11, 5.0, 0, 0, 0, 0, 0, 0],
        [2 * DAY, 11, 13, 10, 12, 6.0, 0, 0, 0, 0, 0, 0],
        [3 * DAY, 12, 14, 11, 13, 7.0, 0, 0, 0, 0, 0, 0],
        [4 * DAY, 13, 13, 13, 13, 0.0, 0, 0, 0, 0, 0, 0],
    ]
    _write_csv(tmp_path, "EOSUSDT", "1d", "1970-01", rows)
    m = pit_universe.PITManifest.from_records(
        [{"symbol": "EOSUSDT", "listed_from": 1 * DAY, "delisted_at": 4 * DAY}]
    )
    bars = pit_bars.load_bars("EOSUSDT", "1d", m, root=tmp_path)
    assert [b.ts for b in bars] == [1 * DAY, 2 * DAY, 3 * DAY]
    assert bars[0].close == 11 and bars[-1].close == 13


def test_load_bars_unknown_symbol_returns_empty(tmp_path):
    m = pit_universe.PITManifest.from_records([{"symbol": "X", "listed_from": 0}])
    assert pit_bars.load_bars("X", "1d", m, root=tmp_path) == []


def test_load_panel_aligns_close_series(tmp_path):
    _write_csv(tmp_path, "AUSDT", "1d", "1970-01",
               [[1 * DAY, 1, 1, 1, 100, 5, 0, 0, 0, 0, 0, 0],
                [2 * DAY, 1, 1, 1, 110, 5, 0, 0, 0, 0, 0, 0]])
    _write_csv(tmp_path, "BUSDT", "1d", "1970-01",
               [[1 * DAY, 1, 1, 1, 200, 5, 0, 0, 0, 0, 0, 0],
                [2 * DAY, 1, 1, 1, 220, 5, 0, 0, 0, 0, 0, 0]])
    m = pit_universe.PITManifest.from_records([
        {"symbol": "AUSDT", "listed_from": 0}, {"symbol": "BUSDT", "listed_from": 0},
    ])
    panel = pit_bars.load_panel(["AUSDT", "BUSDT"], "1d", m, root=tmp_path)
    assert panel["AUSDT"] == [(1 * DAY, 100.0), (2 * DAY, 110.0)]
    assert panel["BUSDT"] == [(1 * DAY, 200.0), (2 * DAY, 220.0)]
