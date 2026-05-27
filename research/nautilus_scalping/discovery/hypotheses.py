"""ROB-339 — hypothesis evaluation machinery + the five family functions (pure).

``evaluate_hypothesis`` is the auditable core: chronological in-sample/OOS split,
fee-adjusted in-sample expectancy (decision metric), OOS-tail confirmation. Each
family is a thin boolean condition mask over the engineered features. Conditions
are intentionally simple first cuts (refine in PR2); discovery only screens.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from discovery.screen import HypothesisSummary

_BPS = 1e4
# Maker passive entry saves ~the taker/maker spread on the entry leg; for a
# conservative screen we still budget the taker round-trip and let missed_fill
# carry the maker-specific risk. Precise maker re-sim is PR2.


def evaluate_hypothesis(
    bars: pd.DataFrame,
    signal_mask: pd.Series,
    *,
    name: str,
    conditions: str,
    fwd_col: str,
    direction: str = "long",
    fee_budget_bps: float,
    oos_frac: float = 0.25,
    missed_fill_ratio: float | None = None,
    regime: str | None = None,
    time_bucket: str | None = None,
    symbol: str | None = None,
) -> HypothesisSummary:
    df = bars.reset_index(drop=True)
    mask = signal_mask.reset_index(drop=True).fillna(False).astype(bool)
    outcome = df[fwd_col].astype(float)
    if direction == "short":
        outcome = -outcome

    n = len(df)
    split_idx = int(n * (1.0 - oos_frac))
    pos = pd.Series(range(n))
    valid = mask & outcome.notna()
    in_out = outcome[valid & (pos < split_idx)]
    oos_out = outcome[valid & (pos >= split_idx)]

    gross = float(in_out.mean()) if len(in_out) else 0.0
    oos_fee_adj = float(oos_out.mean()) - fee_budget_bps if len(oos_out) else None
    return HypothesisSummary(
        name=name,
        conditions=conditions,
        sample_count=int(len(in_out)),
        gross_expectancy_bps=gross,
        fee_adjusted_bps=gross - fee_budget_bps,
        oos_fee_adjusted_bps=oos_fee_adj,
        missed_fill_ratio=missed_fill_ratio,
        regime=regime,
        time_bucket=time_bucket,
        symbol=symbol,
    )


def _maker_missed_fill_ratio(
    featured: pd.DataFrame, mask: pd.Series, entry_offset_bps: float
) -> float:
    sig = featured[mask.fillna(False).astype(bool)]
    nxt = sig["next_low"]
    valid = nxt.notna()
    if valid.sum() == 0:
        return 0.0
    limit = sig["close"] * (1.0 - entry_offset_bps / _BPS)
    return float((nxt[valid] > limit[valid]).mean())


def run_all_hypotheses(
    featured: pd.DataFrame,
    *,
    fee_budget_bps: float,
    entry_offset_bps: float = 5.0,
    oos_frac: float = 0.25,
    symbol: str | None = None,
) -> list[HypothesisSummary]:
    """Evaluate the five hypothesis families against engineered features."""
    f = featured
    out: list[HypothesisSummary] = []

    # 1. momentum continuation: recent up-extension, close near high, volume expansion
    momo = (f["ret_3m"] > 0) & (f["close_pos"] >= 0.6) & (f["vol_z"] > 0.0)
    out.append(
        evaluate_hypothesis(
            f,
            momo,
            name="momentum_continuation",
            conditions="ret_3m>0 & close_pos>=0.6 & vol_z>0",
            fwd_col="fwd_ret_3m",
            direction="long",
            fee_budget_bps=fee_budget_bps,
            oos_frac=oos_frac,
        )
    )

    # 2. liquidity sweep / fake-breakout reversal: prior low swept then reclaimed
    sweep = (f["low"] < f["roll_low"]) & (f["close"] > f["roll_low"])
    out.append(
        evaluate_hypothesis(
            f,
            sweep,
            name="sweep_reversal",
            conditions="low<roll_low & close>roll_low (sweep+reclaim)",
            fwd_col="fwd_ret_3m",
            direction="long",
            fee_budget_bps=fee_budget_bps,
            oos_frac=oos_frac,
        )
    )

    # 3. volatility regime filter: momentum restricted to the high-vol regime
    vol = (f["vol_bucket"] == "high") & (f["ret_3m"] > 0)
    out.append(
        evaluate_hypothesis(
            f,
            vol,
            name="vol_regime_filter",
            conditions="vol_bucket=='high' & ret_3m>0",
            fwd_col="fwd_ret_3m",
            direction="long",
            regime="high",
            fee_budget_bps=fee_budget_bps,
            oos_frac=oos_frac,
        )
    )

    # 4. time-of-day filter: momentum restricted to the US session window
    tod = (f["time_bucket"] == "us") & (f["ret_3m"] > 0)
    out.append(
        evaluate_hypothesis(
            f,
            tod,
            name="time_of_day_filter",
            conditions="time_bucket=='us' & ret_3m>0",
            fwd_col="fwd_ret_3m",
            direction="long",
            time_bucket="us",
            fee_budget_bps=fee_budget_bps,
            oos_frac=oos_frac,
        )
    )

    # 5. maker/passive-entry viability: momentum signal + passive-fill feasibility
    missed = _maker_missed_fill_ratio(f, momo, entry_offset_bps)
    out.append(
        evaluate_hypothesis(
            f,
            momo,
            name="maker_viability",
            conditions=f"momentum signal w/ passive limit {entry_offset_bps}bps below close",
            fwd_col="fwd_ret_3m",
            direction="long",
            fee_budget_bps=fee_budget_bps,
            oos_frac=oos_frac,
            missed_fill_ratio=missed,
        )
    )

    if symbol is not None:
        out = [replace(s, symbol=symbol) for s in out]
    return out
