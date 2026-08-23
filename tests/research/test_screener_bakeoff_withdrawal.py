"""Regression tests for withdrawn historical comparators."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.screener_bakeoff import aggregate, bootstrap, report, run_bakeoff
from research.screener_bakeoff.spec import WITHDRAWN_SOURCES


def _pick(source_id: str, ret: float) -> dict:
    return {
        "market": source_id.split(".", 1)[0],
        "source_id": source_id,
        "gate": "none",
        "decision_date": dt.date(2026, 7, 1),
        "symbol": "TEST",
        "rank": 1,
        "horizon": 5,
        "status": "full",
        "entry": 100.0,
        "ret": ret,
        "excess": ret,
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
    withdrawn = next(iter(WITHDRAWN_SOURCES))
    picks = pd.DataFrame(
        [
            _pick("crypto.consecutive_gainers", 0.03),
            _pick(withdrawn, 0.02),
            _pick("crypto.benchmark", 0.01),
        ]
    )

    scorecard, enriched = aggregate.build(picks)
    date_level = aggregate.date_level(enriched)
    bootstrap = pd.DataFrame(
        {
            "market": ["crypto"],
            "source_id": ["crypto.consecutive_gainers"],
            "gate": ["none"],
            "horizon": [5],
            "null_pctile": [0.5],
        }
    )

    assert withdrawn not in set(scorecard["source_id"])
    assert withdrawn not in set(enriched["source_id"])
    assert withdrawn not in set(date_level["source_id"])
    table = report.scorecard_table(date_level, bootstrap, "crypto", 5, "none")
    assert withdrawn not in table

    # The report guard is also exercised independently if a caller bypasses
    # aggregate/date_level and supplies a withdrawn row directly.
    direct = pd.concat(
        [
            date_level,
            pd.DataFrame(
                [
                    {
                        "market": "crypto",
                        "source_id": withdrawn,
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
    assert withdrawn not in report.scorecard_table(
        direct, bootstrap, "crypto", 5, "none"
    )


def test_date_level_excludes_withdrawn_rows_when_called_directly():
    withdrawn = next(iter(WITHDRAWN_SOURCES))
    got = aggregate.date_level(pd.DataFrame([_pick(withdrawn, 0.02)]))

    assert got.empty


def test_bootstrap_excludes_withdrawn_rows_when_called_directly(tmp_path):
    withdrawn = next(iter(WITHDRAWN_SOURCES))
    day = dt.date(2026, 7, 1)
    pd.DataFrame(
        [
            {
                "market": "crypto",
                "horizon": 5,
                "decision_date": day,
                "symbol": f"COIN{i}",
                "ret": 0.01 + i / 1000,
                "status": "full",
            }
            for i in range(10)
        ]
    ).to_csv(tmp_path / "universe_returns.csv", index=False)
    pd.DataFrame(
        [
            _pick(withdrawn, 0.02),
            _pick("crypto.consecutive_gainers", 0.03),
        ]
    ).to_csv(tmp_path / "picks_scored.csv", index=False)

    got = bootstrap.run(tmp_path, draws=8)

    assert withdrawn not in set(got["source_id"])
    assert "crypto.consecutive_gainers" in set(got["source_id"])


def test_gate_matrix_excludes_withdrawn_rows_when_called_directly():
    withdrawn = next(iter(WITHDRAWN_SOURCES))
    dl = pd.DataFrame(
        [
            {
                "market": "crypto",
                "source_id": withdrawn,
                "gate": "none",
                "horizon": 5,
                "window": "all",
                "median_excess": 0.02,
            }
        ]
    )
    sc = pd.DataFrame(
        [
            {
                "market": "crypto",
                "source_id": withdrawn,
                "gate": "none",
                "horizon": 5,
                "window": "all",
                "mean_gated": 1.0,
            }
        ]
    )

    assert withdrawn not in report.gate_matrix(dl, sc, "crypto", 5)


def test_coverage_bias_excludes_withdrawn_rows_when_called_directly():
    withdrawn = next(iter(WITHDRAWN_SOURCES))
    scored = pd.DataFrame(
        [
            {
                "market": "crypto",
                "source_id": withdrawn,
                "gate": "none",
                "horizon": 20,
                "status": "full",
                "censored": False,
                "excess": 0.02,
            }
        ]
    )

    assert "### crypto" not in report.coverage_bias_section(scored)


def test_requested_withdrawn_source_requires_explicit_opt_in():
    withdrawn = next(iter(WITHDRAWN_SOURCES))

    with pytest.raises(ValueError, match="--include-withdrawn-sources"):
        run_bakeoff._validate_requested_sources(
            {withdrawn}, include_withdrawn_sources=False
        )


def test_withdrawn_builder_cannot_be_registered_under_an_alias(monkeypatch):
    monkeypatch.setitem(
        run_bakeoff._CRYPTO_BUILDERS,
        "crypto.tv_rsi45_alias",
        run_bakeoff.S.src_crypto_rsi45,
    )

    with pytest.raises(ValueError, match="canonical source IDs"):
        run_bakeoff._builders_for_market("crypto", None)


def test_default_runner_does_not_build_withdrawn_sources():
    for market in ("kr", "us", "crypto"):
        builders = run_bakeoff._builders_for_market(market, None)
        assert WITHDRAWN_SOURCES.isdisjoint(builders)

    opted_in = run_bakeoff._builders_for_market(
        "crypto", None, include_withdrawn_sources=True
    )
    assert "crypto.tv_rsi45" in opted_in


def test_default_run_market_does_not_call_withdrawn_builder(monkeypatch):
    def assert_no_withdrawn_source_ids(source_ids):
        assert WITHDRAWN_SOURCES.isdisjoint(source_ids)
        assert not {
            source_id for source_id in source_ids if source_id.endswith(".tv_rsi45")
        }

    def live_builder(_ctx, _day):
        return ["EVIDENCE"]

    pools = {}
    for name, market in (
        ("_KR_BUILDERS", "kr"),
        ("_US_BUILDERS", "us"),
        ("_CRYPTO_BUILDERS", "crypto"),
    ):
        monkeypatch.setattr(
            run_bakeoff,
            name,
            {f"{market}.consecutive_gainers": live_builder},
        )
        pools[market] = run_bakeoff._builders_for_market(market, None)
        assert_no_withdrawn_source_ids(pools[market])

    monkeypatch.setattr(run_bakeoff.S, "src_random", lambda *_args: [])
    monkeypatch.setattr(run_bakeoff.S, "src_benchmark", lambda *_args: [])
    monkeypatch.setattr(
        run_bakeoff.S,
        "evaluate_gate",
        lambda *_args: run_bakeoff.S.GateEvidence(40.0, False, 0),
    )
    monkeypatch.setattr(
        run_bakeoff.scoring,
        "score",
        lambda *_args: SimpleNamespace(
            status="full",
            entry=100.0,
            ret=0.01,
            mfe=0.01,
            mae=0.0,
            ret_hl_mfe=0.01,
            ret_hl_mae=0.0,
            bars_used=1,
        ),
    )

    day = dt.date(2026, 7, 1)
    contexts = {
        "kr": SimpleNamespace(
            market="kr",
            screener={day: pd.DataFrame({"symbol": ["KR"]})},
            prices=SimpleNamespace(calendar=np.array([day], dtype=object)),
        ),
        "us": SimpleNamespace(
            market="us",
            screener={day: pd.DataFrame({"symbol": ["US"]})},
            prices=SimpleNamespace(calendar=np.array([day], dtype=object)),
        ),
        "crypto": SimpleNamespace(
            market="crypto",
            crypto={
                day: pd.DataFrame(
                    {
                        "symbol": ["BTC"],
                        "rsi": [40.0],
                        "trade_amount_24h": [1_000_000_000.0],
                    }
                )
            },
            prices=SimpleNamespace(calendar=np.array([day], dtype=object)),
        ),
    }
    out_rows = []
    for _market, ctx in contexts.items():
        before = len(out_rows)
        run_bakeoff.run_market(ctx, [day], out_rows, {}, wanted_sources=None)
        market_rows = out_rows[before:]
        assert_no_withdrawn_source_ids({row["source_id"] for row in market_rows})

    assert_no_withdrawn_source_ids({row["source_id"] for row in out_rows})
