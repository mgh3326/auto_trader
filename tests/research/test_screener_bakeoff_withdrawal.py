"""Regression tests for withdrawn historical comparators."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research.screener_bakeoff import aggregate, report, run_bakeoff
from research.screener_bakeoff.spec import WITHDRAWN_SOURCES


def _pick(source_id: str, ret: float) -> dict:
    return {
        "market": "kr",
        "source_id": source_id,
        "gate": "none",
        "decision_date": dt.date(2026, 7, 1),
        "symbol": "TEST",
        "rank": 1,
        "horizon": 5,
        "status": "full",
        "entry": 100.0,
        "ret": ret,
        "mfe": ret,
        "mae": ret,
        "hl_mfe": ret,
        "hl_mae": ret,
        "bars_used": 5,
        "sessions_after": 99,
        "censored": False,
        "window": "earlier",
        "pool_size": 1,
        "gated_size": 1,
    }


def test_withdrawn_pick_cannot_reappear_in_aggregate_date_level_or_report():
    picks = pd.DataFrame(
        [
            _pick("kr.consecutive_gainers", 0.03),
            _pick("kr.tv_rsi45", 0.02),
            _pick("kr.benchmark", 0.01),
        ]
    )

    scorecard, enriched = aggregate.build(picks)
    date_level = aggregate.date_level(enriched)
    bootstrap = pd.DataFrame(
        {
            "market": ["kr"],
            "source_id": ["kr.consecutive_gainers"],
            "gate": ["none"],
            "horizon": [5],
            "null_pctile": [0.5],
        }
    )

    assert "kr.tv_rsi45" not in set(scorecard["source_id"])
    assert "kr.tv_rsi45" not in set(enriched["source_id"])
    assert "kr.tv_rsi45" not in set(date_level["source_id"])
    table = report.scorecard_table(date_level, bootstrap, "kr", 5, "none")
    assert "kr.tv_rsi45" not in table

    # The report guard is also exercised independently if a caller bypasses
    # aggregate/date_level and supplies a withdrawn row directly.
    direct = pd.concat(
        [
            date_level,
            pd.DataFrame(
                [
                    {
                        "market": "kr",
                        "source_id": "kr.tv_rsi45",
                        "gate": "none",
                        "horizon": 5,
                        "window": "all",
                        "dates": 1,
                        "mean_ret": 0.02,
                        "median_ret": 0.02,
                        "mean_excess": 0.01,
                        "median_excess": 0.01,
                        "dates_beating_market": 1.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    assert "kr.tv_rsi45" not in report.scorecard_table(
        direct, bootstrap, "kr", 5, "none"
    )


def test_default_runner_does_not_build_withdrawn_sources():
    for market in ("kr", "us", "crypto"):
        builders = run_bakeoff._builders_for_market(market, None)
        assert WITHDRAWN_SOURCES.isdisjoint(builders)

    opted_in = run_bakeoff._builders_for_market(
        "crypto", None, include_withdrawn_sources=True
    )
    assert "crypto.tv_rsi45" in opted_in


def test_default_run_market_does_not_call_withdrawn_builder(monkeypatch):
    called: list[str] = []

    def builder(source_id):
        def _build(_ctx, _day):
            called.append(source_id)
            return []

        return _build

    monkeypatch.setattr(
        run_bakeoff,
        "_CRYPTO_BUILDERS",
        {source_id: builder(source_id) for source_id in run_bakeoff._CRYPTO_BUILDERS},
    )
    monkeypatch.setattr(run_bakeoff.S, "src_random", lambda *_args: [])
    monkeypatch.setattr(run_bakeoff.S, "src_benchmark", lambda *_args: [])

    day = dt.date(2026, 7, 1)
    ctx = SimpleNamespace(
        market="crypto",
        prices=SimpleNamespace(calendar=np.array([day], dtype=object)),
    )
    run_bakeoff.run_market(ctx, [day], {}, [], {}, wanted_sources=None)

    assert WITHDRAWN_SOURCES.isdisjoint(called)
