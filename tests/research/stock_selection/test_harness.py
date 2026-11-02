"""Leakage, session boundaries, hand-calculated scoring, and CLI persistence."""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.stock_selection.dataset import (
    FEATURES,
    HORIZONS,
    MODEL_FEATURES,
    LeakageError,
    audit_dataset,
    build_dataset,
    decision_at,
    sessions,
)
from research.stock_selection.forward import make_lists
from research.stock_selection.io import digest, read_bars, write_json
from research.stock_selection.metrics import score_daily
from research.stock_selection.score import grade_lists
from research.stock_selection.splits import walk_forward

pytestmark = pytest.mark.unit


@pytest.fixture(params=["kr", "us", "crypto"])
def panel(request):
    market = request.param
    days = sessions(market, "2020-01-02", "2021-08-31")
    records = []
    for s, symbol in enumerate(["__benchmark__", *[f"S{i:02}" for i in range(12)]]):
        close = 100 * np.exp(
            np.arange(len(days)) * (s + 1) / 20000
            + 0.02 * np.sin(np.arange(len(days)) / (4 + s))
        )
        for i, day in enumerate(days):
            records.append(
                {
                    "date": day.date().isoformat(),
                    "symbol": symbol,
                    "open": close[i] * 0.999,
                    "high": close[i] * 1.01,
                    "low": close[i] * 0.99,
                    "close": close[i],
                    "volume": 10000 + s * 100 + i,
                    "traded_value": close[i] * (10000 + s * 100 + i),
                    "action": False,
                    "available_at": decision_at(market, day.date()).isoformat(),
                }
            )
    return market, pd.DataFrame(records)


def test_features_are_prefix_invariant_and_future_timestamp_is_rejected(panel):
    market, bars = panel
    day = "2021-06-15"
    full = build_dataset(bars, market)
    prefix = build_dataset(bars[bars.date <= day], market)
    cols = ["date", "symbol", *MODEL_FEATURES, "liquidity_20", "eligible"]
    pd.testing.assert_frame_equal(
        full[full.date <= day][cols].reset_index(drop=True), prefix[cols]
    )
    # An extreme bar in the future cannot affect already-emitted features.
    mutated = bars.copy()
    mutated.loc[mutated.date > day, ["open", "high", "low", "close"]] *= 100
    after = build_dataset(mutated, market)
    pd.testing.assert_frame_equal(
        full[full.date <= day][cols].reset_index(drop=True),
        after[after.date <= day][cols].reset_index(drop=True),
    )
    bad = bars.copy()
    # Inject an actual future bar into the first decision, retaining that
    # future bar's availability. Its prices cannot be used at the earlier t.
    future_bar = bars[bars.symbol == bars.iloc[0].symbol].iloc[-1].copy()
    future_bar["date"] = bars.iloc[0].date
    bad.loc[0] = future_bar
    with pytest.raises(LeakageError, match="available_at"):
        build_dataset(bad, market)


def test_audit_detects_future_feature_and_label_injections(panel):
    market, bars = panel
    data = build_dataset(bars, market)
    audit_dataset(bars, data, market)
    for col in ("momentum_20", "return_3"):
        bad = data.copy()
        index = bad[bad.eligible].index[0]
        row = bad.loc[index]
        prices = bars[bars.symbol == row.symbol].set_index("date").close
        if col == "momentum_20":
            # Deliberately leaky: replace today's momentum by t+3 return.
            future = row.label_end_3.date().isoformat()
        else:
            # A nominal 3-session label must not use t+5's future close.
            future = row.label_end_5.date().isoformat()
        bad.loc[index, col] = prices[future] / prices[row.date.date().isoformat()] - 1
        with pytest.raises(LeakageError, match="lineage"):
            audit_dataset(bars, bad, market)


def test_calendar_horizons_missing_bars_and_actions_remain_unknown(panel):
    market, bars = panel
    data = build_dataset(bars, market)
    day = pd.Timestamp("2021-06-15")
    row = data[(data.date == day) & (data.symbol == "S00")].iloc[0]
    for h in HORIZONS:
        end = row[f"label_end_{h}"].date().isoformat()
        prices = bars[bars.symbol == "S00"].set_index("date").close
        assert row[f"return_{h}"] == pytest.approx(
            prices[end] / prices[day.date().isoformat()] - 1
        )
    next_day = row.label_end_1.date().isoformat()
    absent = bars[~((bars.symbol == "S00") & (bars.date == next_day))]
    changed = build_dataset(absent, market)
    got = changed[(changed.date == day) & (changed.symbol == "S00")].iloc[0]
    assert np.isnan(got.return_1) and np.isnan(got.return_3)
    assert got.eligible  # unknown future must not exclude today's candidate
    action = bars.copy()
    action.loc[(action.symbol == "S00") & (action.date == next_day), "action"] = True
    got = (
        build_dataset(action, market).query("date == @day and symbol == 'S00'").iloc[0]
    )
    assert np.isnan(got.return_1) and np.isnan(got.return_10)


def test_52_week_high_uses_equity_sessions_or_crypto_calendar_days(panel):
    market, bars = panel
    day = "2021-06-15"
    asset = bars[(bars.symbol == "S00") & (bars.date <= day)]
    peak_index = asset.index[-301]
    changed = bars.copy()
    changed.loc[peak_index, "high"] = 1_000_000
    dataset = build_dataset(changed, market)
    row = dataset.loc[
        (dataset.date == pd.Timestamp(day)) & (dataset.symbol == "S00")
    ].iloc[0]
    expected_window = 364 if market == "crypto" else 252
    history = changed[(changed.symbol == "S00") & (changed.date <= day)]
    expected = history.iloc[-1].close / history.high.iloc[-expected_window:].max() - 1
    assert row.high_distance_52w == pytest.approx(expected)
    assert (row.high_distance_52w < -0.99) == (market == "crypto")


def test_walk_forward_adds_purge_and_embargo_on_common_dates():
    days = pd.bdate_range("2020-01-01", periods=160)
    folds = walk_forward(days, first_test=days[100].isoformat(), test_sessions=20)
    assert folds[0].train_dates[-1] == days[79]
    assert folds[0].test_dates[0] == days[100]
    assert len(folds[1].train_dates) == len(folds[0].train_dates) + 20
    # Uneven panel density must not turn the 10-session gap into 10 rows.
    panel_dates = [
        day for index, day in enumerate(days) for _ in range(2 + index % 101)
    ]
    panel_folds = walk_forward(
        panel_dates, first_test=days[100].isoformat(), test_sessions=20
    )
    pd.testing.assert_index_equal(panel_folds[0].train_dates, folds[0].train_dates)
    for h in HORIZONS:
        label_end = days.get_loc(panel_folds[0].train_dates[-1]) + h
        assert days.get_loc(panel_folds[0].test_dates[0]) - label_end - 1 >= 10
    with pytest.raises(ValueError, match="max horizon"):
        walk_forward(days, first_test="2020-06-01", embargo=9)


def test_scorer_matches_hand_computed_ic_spread_hit_cost_and_turnover():
    # Rank vectors [1,2,3,...,10], [2,1,3,...,10]: sum d^2=2.
    # Spearman = 1 - 6*2/(10*(10^2-1)) = 163/165.
    returns = np.array(
        [-0.04, -0.05, -0.03, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05]
    )
    frame = pd.DataFrame(
        {
            "date": pd.Timestamp("2022-01-03"),
            "symbol": [f"S{i}" for i in range(10)],
            "score": np.arange(10),
            "return_1": returns,
            "relative_return_1": returns - 0.02,
            "regime": "up",
        }
    )
    second = frame.assign(date=pd.Timestamp("2022-01-04"))
    result = score_daily(pd.concat([frame, second]), market="us", horizon=1, top_n=2)
    summary = result["summary"]
    assert summary["ic"] == pytest.approx(163 / 165, abs=1e-15)
    assert summary["icir"] is None  # zero dispersion is undefined, not infinity
    assert summary["decile_spread"] == pytest.approx(0.09, abs=1e-15)
    assert summary["cost_adjusted_spread"] == pytest.approx(0.086, abs=1e-15)
    assert summary["up_hit"] == summary["down_hit"] == 1
    assert [d["turnover"] for d in result["daily"]] == [1, 0]
    relative = score_daily(frame, market="us", horizon=1, relative=True)
    assert relative["summary"]["ic"] == pytest.approx(163 / 165)
    assert relative["summary"]["decile_spread"] == pytest.approx(0.09)
    assert result["by_year"]["2022"]["days"] == 2
    assert result["by_regime"]["up"]["days"] == 2


def test_missing_selected_outcome_is_never_silently_dropped():
    frame = pd.DataFrame(
        {
            "date": pd.Timestamp("2022-01-03"),
            "symbol": [f"S{i}" for i in range(10)],
            "score": np.arange(10),
            "return_1": np.arange(10) / 100,
            "regime": "flat",
        }
    )
    frame.loc[9, "return_1"] = np.nan
    summary = score_daily(frame, market="kr", horizon=1)["summary"]
    assert summary["outcome_coverage"] == 0.9
    assert summary["up_hit"] is None
    assert summary["decile_spread"] is None


def test_forward_score_roundtrip_pending_matured_and_revisions(
    panel, tmp_path, monkeypatch, capsys
):
    from research.stock_selection import build, forward, score

    market, bars = panel
    path = tmp_path / market / "bars.csv"
    path.parent.mkdir()
    bars.to_csv(path, index=False)
    write_json(path.with_suffix(".json"), {"market": market, "sha256": digest(path)})
    common = ["--market", market, "--cache-dir", str(tmp_path)]
    monkeypatch.setattr(
        "sys.argv", ["build", *common, "--start", "2021-01-01", "--end", "2021-08-01"]
    )
    build.main()
    assert Path(capsys.readouterr().out.strip()).is_file()
    monkeypatch.setattr(
        "sys.argv", ["forward", *common, "--asof", "2021-06-15", "--top-n", "3"]
    )
    forward.main()
    list_path = Path(capsys.readouterr().out.strip())
    lists = json.loads(list_path.read_text())
    assert set(lists["horizons"]) == {"1", "3", "5", "10"}
    assert len(lists["horizons"]["1"]["up"]) == 3
    assert lists["label_kind"] == "statistical_close_to_close"
    assert lists["prediction_at"] == decision_at(market, date(2021, 6, 15)).isoformat()
    assert lists["data_cutoff"] == "2021-06-15"
    assert lists["execution_label"] is None
    assert (
        grade_lists(bars, lists, asof="2021-06-15", market=market)["horizons"]["1"][
            "state"
        ]
        == "pending"
    )
    monkeypatch.setattr(
        "sys.argv",
        ["score", *common, "--asof", "2021-08-01", "--lists", str(list_path)],
    )
    score.main()
    grade = json.loads(Path(capsys.readouterr().out.strip()).read_text())
    assert all(g["state"] == "scored" for g in grade["horizons"].values())
    assert grade["label_kind"] == lists["label_kind"]
    assert grade["execution_label"] is None
    # A delisted or missing future series never becomes a zero return.
    missing_symbol = lists["horizons"]["1"]["up"][0]["symbol"]
    missing_future = bars[
        ~((bars.symbol == missing_symbol) & (bars.date > "2021-06-15"))
    ]
    unknown = grade_lists(missing_future, lists, asof="2021-08-01", market=market)
    for h in HORIZONS:
        result = unknown["horizons"][str(h)]
        assert result["state"] == "unknown_outcome"
        assert result["raw"]["summary"]["up_hit"] is None
        assert 0 < result["raw"]["summary"]["outcome_coverage"] < 1
    # A stale snapshot graded with a later --asof keeps unmatured horizons
    # pending instead of writing a final unknown_outcome.
    stale = bars[bars.date <= "2021-06-18"]
    early = grade_lists(stale, lists, asof="2021-08-01", market=market)["horizons"]
    assert early["1"]["state"] == "scored"
    assert early["5"]["state"] == "pending"
    assert early["10"]["state"] == "pending"
    with pytest.raises(ValueError, match="separate scorer"):
        grade_lists(
            bars,
            {**lists, "label_kind": "first_tradable_after_prediction"},
            asof="2021-08-01",
            market=market,
        )
    with pytest.raises(ValueError, match="prediction time"):
        grade_lists(
            bars,
            {**lists, "prediction_at": "2021-06-15T00:00:00Z"},
            asof="2021-08-01",
            market=market,
        )
    # Labels from bars after the list date never enter list generation.
    early = make_lists(
        bars[bars.date <= "2021-06-15"], market=market, asof="2021-06-15", top_n=3
    )
    assert early["horizons"] == lists["horizons"]
    changed = bars.copy()
    changed.loc[changed.date == "2021-01-04", "close"] *= 2
    with pytest.raises(ValueError, match="input changed"):
        grade_lists(changed, lists, asof="2021-08-01", market=market)
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_bars(tmp_path, market)


def test_no_app_runtime_or_database_order_scheduler_dependency():
    root = Path(__file__).resolve().parents[3] / "research" / "stock_selection"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not name.startswith(
                    ("sqlalchemy", "asyncpg", "psycopg", "taskiq", "prefect")
                ), (path, name)
                assert not name.startswith("app.") or name == "app.core.symbol", (
                    path,
                    name,
                )
    assert set(FEATURES) < set(MODEL_FEATURES)


def test_evaluation_and_ranker_forward_only_train_before_the_boundary(
    panel, monkeypatch
):
    from research.stock_selection import evaluate, forward

    market, bars = panel
    seen = []

    def predict(train, test, h):
        assert train.date.max() < test.date.min()
        assert train[f"label_end_{h}"].max() < test.date.min()
        seen.append((h, len(train), len(test)))
        return test.momentum_5.to_numpy()

    monkeypatch.setattr(evaluate, "ranker_predict", predict)
    data = build_dataset(bars, market)
    missing_day = pd.Timestamp("2021-04-15")
    data = data[data.date != missing_day]
    result = evaluate.evaluate(
        data,
        market=market,
        train_start="2020-11-01",
        first_test="2021-04-01",
        test_end="2021-04-30",
    )
    assert set(result["results"]) == {"1", "3", "5", "10"}
    assert "2021-04-15" in result["eligibility_audit"]["missing_validation_sessions"]
    assert result["eligibility_audit"]["expected_validation_sessions"] == len(
        sessions(market, "2021-04-01", "2021-04-30")
    )
    for models in result["results"].values():
        assert len(models) == len(MODEL_FEATURES) + 1
        assert len({m["raw"]["summary"]["days"] for m in models.values()}) == 1
    monkeypatch.setattr(forward, "ranker_predict", predict)
    forward.make_lists(
        bars, market=market, asof="2021-06-15", model="lightgbm_rank", top_n=3
    )
    assert len(seen) == 8


def test_naver_source_is_fixed_public_only_and_zero_ohlc_stays_missing(monkeypatch):
    from research.stock_selection import fetch

    calls = []
    body = b'<protocol><chartdata><item data="20190101|100|102|99|101|1000"/><item data="20200102|100|102|99|101|1000"/><item data="20200103|0|0|0|101|0"/><item data="20200104|100|101|99|102|1000"/></chartdata></protocol>'

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params):
            calls.append((url, params))
            return SimpleNamespace(content=body, raise_for_status=lambda: None)

    monkeypatch.setattr(fetch.httpx, "Client", Client)
    rejections = []
    rows = fetch.naver_daily(
        "005930", date(2020, 1, 1), date(2020, 1, 5), rejections=rejections
    )
    assert [r["date"] for r in rows] == ["2020-01-02"]
    assert rows[0]["traded_value"] == 101000  # explicitly a close*volume estimate
    assert [r["reason"] for r in rejections] == [
        "nonpositive_ohlc",
        "inconsistent_ohlc",
    ]
    assert calls[0][0] == "https://fchart.stock.naver.com/sise.nhn"
    assert calls[0][1]["symbol"] == "005930"
    with pytest.raises(ValueError, match="explicit KR"):
        fetch.naver_daily("https://elsewhere/", date(2020, 1, 1), date(2020, 1, 4))
    assert len(calls) == 1


def test_corporate_action_survives_a_missing_yahoo_price_bar():
    from research.stock_selection.fetch import yahoo

    stamp = int(pd.Timestamp("2022-01-03T00:00:00Z").timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [stamp],
                    "indicators": {
                        "quote": [
                            {
                                key: [None]
                                for key in ("open", "high", "low", "close", "volume")
                            }
                        ]
                    },
                    "events": {"dividends": {str(stamp): {"date": stamp, "amount": 1}}},
                }
            ]
        }
    }
    events = set()
    rows = yahoo(
        lambda url: SimpleNamespace(status=200, body=json.dumps(payload).encode()),
        "005930.KS",
        date(2022, 1, 1),
        date(2022, 1, 5),
        event_dates=events,
    )
    assert rows == []
    assert events == {"2022-01-03"}  # Naver's bar must retain this action flag
