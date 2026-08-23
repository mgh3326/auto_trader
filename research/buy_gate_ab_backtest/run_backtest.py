"""Driver: reconstruct → evaluate (A/B) → score → aggregate. Offline only.

    uv run python -m research.buy_gate_ab_backtest.run_backtest --market kr

Writes one JSON result file per market. Reads frozen parquet corpora and
nothing else.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Any

import numpy as np
import pandas as pd

from app.services.buy_gate_ab_shadow.evaluate import (
    CandidateEvidence,
    evaluate_candidate,
)
from app.services.buy_gate_ab_shadow.scoring import DailyBar, ScoringError, score_window
from app.services.buy_gate_ab_shadow.spec import PRE_REGISTRATION, spec_sha256
from research.buy_gate_ab_backtest import corpus
from research.buy_gate_ab_backtest.preregistration import (
    ADDENDUM,
    FIRST_FREEZE_ADDENDUM_SHA256,
    addendum_sha256,
)
from research.buy_gate_ab_backtest.reconstruct import (
    RSI_WINDOW_BARS,
    ReconstructionFailure,
    build_evidence,
)

WINDOWS: tuple[int, ...] = tuple(PRE_REGISTRATION["windows_trading_days"])
# S2 -- read from the digest-covered addendum, not restated here.
CADENCE: int = int(ADDENDUM["constants"]["cadence_sessions"])
LIQUIDITY_LOOKBACK: int = int(ADDENDUM["constants"]["liquidity_lookback_sessions"])
_FLOORS = ADDENDUM["universe"]["liquidity_floor_20d_median_traded_value"]

COHORTS = ("a_and_b", "b_only", "neither")


def _floor_for(market: str) -> float:
    if market == "kr":
        return float(_FLOORS["kr"])
    if market == "us":
        return float(_FLOORS["us"])
    return float(_FLOORS[f"crypto_{market.removeprefix('crypto_')}"])


def _bars_for_scoring(
    frame: pd.DataFrame, start: int, count: int
) -> tuple[list[DailyBar], int]:
    """Build forward DailyBars. A corpus bar that violates low<=close<=high is
    dropped and counted — never clamped, never imputed."""
    bars: list[DailyBar] = []
    invalid = 0
    stop = min(start + count, len(frame))
    for pos in range(start, stop):
        row = frame.iloc[pos]
        try:
            bars.append(
                DailyBar(
                    session_date=row["session_date"].date(),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                )
            )
        except ScoringError:
            invalid += 1
            break
    return bars, invalid


def run_market(market: str, *, limit_dates: int | None = None) -> dict[str, Any]:
    started = time.time()
    if market == "kr":
        panel = corpus.load_kr()
    elif market == "us":
        panel = corpus.load_us()
    else:
        panel = corpus.load_crypto(market.removeprefix("crypto_"))

    frame = panel.frame
    scoring_as_of = datetime.combine(
        pd.Timestamp(panel.sessions[-1]).date(), datetime.min.time(), tzinfo=UTC
    )
    phase_sessions = panel.sessions[::CADENCE]
    # S3 -- the first phase dates cannot yield a sample because no symbol has
    # 250 prior sessions yet. They were counted as decision dates before, which
    # overstated the sampling grid. Both numbers are now reported.
    eligible_phase_sessions = tuple(
        session
        for index, session in enumerate(panel.sessions)
        if index % CADENCE == 0 and index >= RSI_WINDOW_BARS - 1
    )
    decision_sessions = set(eligible_phase_sessions)
    if limit_dates is not None:
        decision_sessions = set(sorted(decision_sessions)[-limit_dates:])
    # How many market sessions follow each decision date, for B2 censoring.
    session_rank = {session: index for index, session in enumerate(panel.sessions)}
    last_index = len(panel.sessions) - 1
    global_forward_sessions = {
        session: last_index - session_rank[session] for session in decision_sessions
    }
    floor = _floor_for(market)
    max_window = max(WINDOWS)

    samples: list[dict[str, Any]] = []
    failures: Counter[str] = Counter()
    universe_rows = 0
    liquidity_rejects = 0
    history_rejects = 0

    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.reset_index(drop=True)
        if len(group) < RSI_WINDOW_BARS + 1:
            continue
        dates = group["session_date"].to_numpy()
        traded_value = group["value"].rolling(LIQUIDITY_LOOKBACK).median().to_numpy()
        for pos in range(RSI_WINDOW_BARS - 1, len(group)):
            if dates[pos] not in decision_sessions:
                continue
            universe_rows += 1
            if pos < RSI_WINDOW_BARS - 1:
                history_rejects += 1
                continue
            liquidity = traded_value[pos]
            if not np.isfinite(liquidity) or liquidity < floor:
                liquidity_rejects += 1
                continue
            decision_day = pd.Timestamp(dates[pos]).date()
            evidence = build_evidence(
                symbol=symbol,
                market=market,
                bars=group.iloc[: pos + 1],
                decision_date=decision_day,
            )
            if isinstance(evidence, ReconstructionFailure):
                failures[evidence.reason] += 1
                continue
            annex = {
                key: evidence.pop(key) for key in list(evidence) if key.startswith("_")
            }
            evaluation = evaluate_candidate(
                CandidateEvidence.from_mapping(evidence),
                evaluation_as_of=datetime.combine(
                    decision_day, datetime.min.time(), tzinfo=UTC
                ),
            )
            forward, invalid = _bars_for_scoring(group, pos + 1, max_window)
            scores = {
                window: score_window(
                    entry=evaluation.entry_price,
                    bars=forward,
                    decision_date=decision_day,
                    scoring_as_of=scoring_as_of,
                    window_trading_days=window,
                )
                for window in WINDOWS
            }
            samples.append(
                {
                    "symbol": symbol,
                    "decision_date": str(pd.Timestamp(dates[pos]).date()),
                    "cohort": evaluation.cohort,
                    "support_strength": evaluation.support_strength,
                    "rsi": str(evaluation.input_snapshot["rsi"]),
                    "support_distance_pct": evaluation.input_snapshot[
                        "support_distance_pct"
                    ],
                    "entry_price": str(evaluation.entry_price),
                    "shared_reject_reasons": list(evaluation.shared_reject_reasons),
                    "variant_a_passed": evaluation.variant_a.passed,
                    "variant_b_passed": evaluation.variant_b.passed,
                    "support_family_count": annex.get("_support_family_count"),
                    "support_resistance_computed": annex.get(
                        "_support_resistance_computed"
                    ),
                    "invalid_forward_bars": invalid,
                    # B2 censoring inputs: how many forward bars this symbol
                    # actually has, against how many sessions the market ran
                    # after the decision. Equal-and-short means corpus end;
                    # short-while-the-market-kept-trading means the symbol
                    # stopped, which is informative censoring, not truncation.
                    "forward_bars_available": len(forward),
                    "global_forward_sessions": int(
                        global_forward_sessions.get(dates[pos], 0)
                    ),
                    "scores": {
                        str(window): score.as_dict() for window, score in scores.items()
                    },
                }
            )

    return {
        "market": market,
        "corpus_id": panel.corpus_id,
        "corpus_files_read": panel.files_read,
        "corpus_rows": int(len(frame)),
        "corpus_symbols": int(frame["symbol"].nunique()),
        "corpus_first_session": str(pd.Timestamp(panel.sessions[0]).date()),
        "corpus_last_session": str(pd.Timestamp(panel.sessions[-1]).date()),
        "scoring_as_of": scoring_as_of.isoformat(),
        "phase_sessions_total": len(phase_sessions),
        "decision_sessions": len(decision_sessions),
        "decision_sessions_note": (
            "eligible phase dates only; phase_sessions_total includes the "
            "leading dates with under 250 prior sessions, which can produce "
            "no sample (S3)"
        ),
        "universe_rows_at_decision_dates": universe_rows,
        "rejected_insufficient_history": history_rejects,
        "rejected_below_liquidity_floor": liquidity_rejects,
        "liquidity_floor": floor,
        "reconstruction_failures": dict(failures),
        "upstream_spec_sha256": spec_sha256(),
        "addendum_sha256": addendum_sha256(),
        "first_freeze_addendum_sha256": FIRST_FREEZE_ADDENDUM_SHA256,
        "elapsed_seconds": round(time.time() - started, 1),
        "samples": samples,
    }


# ---------------------------------------------------------------------------
# aggregation (additive only; per-sample numbers come from score_window)
# ---------------------------------------------------------------------------


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 6),
        "median": round(median(values), 6),
        "p10": round(ordered[max(0, int(0.10 * (len(ordered) - 1)))], 6),
        "p90": round(ordered[min(len(ordered) - 1, int(0.90 * (len(ordered) - 1)))], 6),
    }


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    by_cohort: dict[str, dict[str, Any]] = {}
    for cohort in COHORTS:
        rows = [row for row in result["samples"] if row["cohort"] == cohort]
        windows: dict[str, Any] = {}
        for window in WINDOWS:
            key = str(window)
            scoreable = [row for row in rows if row["scores"][key]["scoreable"]]
            returns = [
                float(row["scores"][key]["primary"]["simple_return_to_close"])
                for row in scoreable
            ]
            drawdowns = [
                float(
                    row["scores"][key]["primary"]["max_drawdown_from_entry_close_peak"]
                )
                for row in scoreable
            ]
            highs = [
                float(row["scores"][key]["sensitivity"]["simple_return_to_window_high"])
                for row in scoreable
            ]
            lows = [
                float(row["scores"][key]["sensitivity"]["simple_return_to_window_low"])
                for row in scoreable
            ]
            windows[key] = {
                "n_submitted": len(rows),
                "n_scoreable": len(scoreable),
                "simple_return_to_close": _stats(returns),
                "win_rate_return_gt_zero": (
                    round(sum(1 for value in returns if value > 0) / len(returns), 6)
                    if returns
                    else None
                ),
                "max_drawdown_from_entry_close_peak": _stats(drawdowns),
                "sensitivity_return_to_window_high": _stats(highs),
                "sensitivity_return_to_window_low": _stats(lows),
            }
        by_cohort[cohort] = {
            "n": len(rows),
            "support_strength_histogram": dict(
                Counter(row["support_strength"] for row in rows)
            ),
            "distinct_symbols": len({row["symbol"] for row in rows}),
            "windows": windows,
        }
    reject_reasons = Counter()
    for row in result["samples"]:
        if row["cohort"] == "neither":
            for reason in row["shared_reject_reasons"] or ["support_strength_only"]:
                reject_reasons[reason] += 1
    return {
        "market": result["market"],
        "cohort_labels": {
            "a_and_b": "variant A pass (live gate)",
            "b_only": "B-only admit — the B minus A set",
            "neither": "control: rejected by both variants",
        },
        "cohorts": by_cohort,
        "control_reject_reason_histogram": dict(reject_reasons),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--market",
        required=True,
        choices=["kr", "us", "crypto_upbit_krw", "crypto_binance_usdt_spot"],
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit-dates", type=int, default=None)
    parser.add_argument("--no-samples", action="store_true")
    args = parser.parse_args()

    result = run_market(args.market, limit_dates=args.limit_dates)
    result["summary"] = summarize(result)
    if args.no_samples:
        result.pop("samples")
    out = args.out or f"/tmp/gatebt-{args.market}.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"\nwrote {out}  ({result['elapsed_seconds']}s)")


if __name__ == "__main__":
    main()
