"""Build ``01_cohort_enriched.csv`` (rev2 §1) from one reconstruction per row."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from research.rob1383_buy_drought.core import (
    Reconstruction,
    build_variant,
    recomputed_support_failures,
)
from research.rob1383_buy_drought.gates import (
    EPISODE_ANCHOR_TOL_PCT,
    EPISODE_CRYPTO_HOURS,
    EPISODE_SESSIONS_MAX,
    EpisodeInput,
    assign_episodes,
    compose,
    union_gates,
)
from research.rob1383_buy_drought.inputs import Rejection
from research.rob1383_buy_drought.prices import PriceStore

BASELINE = build_variant("baseline")


def _sessions_between(store: PriceStore):
    def count(market: str, symbol: str, t1: datetime, t2: datetime) -> int | None:
        bars = store.bars(market, symbol)
        if bars is None:
            return None
        ts = bars["close_ts"]
        return int(((ts > pd.Timestamp(t1)) & (ts <= pd.Timestamp(t2))).sum())

    return count


def build(
    rejections: Sequence[Rejection],
    recon: dict[int, Reconstruction | str],
    store: PriceStore,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    episode_inputs: list[EpisodeInput] = []
    for rej in rejections:
        rc = recon.get(rej.row)
        rec_fail = None
        best_level = None
        recon_rsi = None
        current = None
        basis = None
        missing = rc if isinstance(rc, str) else None
        if isinstance(rc, Reconstruction):
            rec_fail, best_level = recomputed_support_failures(
                rej.market, rc.supports, rc.current_price, BASELINE
            )
            recon_rsi, current, basis = rc.rsi, rc.current_price, rc.price_basis
        verdict = compose(rej.reason, rec_fail, recon_rsi)
        union = union_gates(verdict.failed_text, verdict.failed_recomputed)
        text_s = set(verdict.failed_text)
        rec_s = (
            set(union_gates(frozenset(), verdict.failed_recomputed))
            if rec_fail is not None
            else None
        )
        anchor = best_level.price if best_level is not None else rej.target_price
        episode_inputs.append(
            EpisodeInput(rej.row, rej.market, rej.symbol, rej.created_at, anchor)
        )
        rows.append(
            {
                "row": rej.row,
                "forecast_id": rej.forecast_id,
                "market": rej.market,
                "symbol": rej.symbol,
                "created_at_utc": rej.created_at.isoformat(),
                "session_label": rej.session_label,
                "policy_version": rej.policy_version,
                "v0_primary": rej.primary,
                "v0_labels": "|".join(sorted(rej.labels)),
                "failed_gates": "|".join(sorted(union)),
                "failed_gates_text": "|".join(sorted(verdict.failed_text)),
                "failed_gates_recomputed": (
                    "|".join(sorted(verdict.failed_recomputed))
                    if verdict.failed_recomputed is not None
                    else None
                ),
                "failed_gates_source": (
                    "text+recomputed"
                    if rec_s is not None and text_s and rec_s
                    else "text"
                    if text_s
                    else "recomputed"
                    if rec_s
                    else "none"
                ),
                "text_recomputed_agree": (
                    None
                    if rec_s is None
                    else (text_s - {"other", "upside_min", "data_fail"}) == rec_s
                ),
                "other_keywords": "|".join(verdict.other_keywords),
                "support_only_fail": verdict.support_only_fail,
                "support_only_unverified": verdict.support_only_basis
                == "recomputed_text_silent",
                "support_only_basis": verdict.support_only_basis,
                "rsi_value": verdict.rsi_value,
                "rsi_source": verdict.rsi_source,
                "recon_rsi": recon_rsi,
                "recon_current_price": current,
                "recon_price_basis": basis,
                "recon_best_support_price": best_level.price if best_level else None,
                "recon_best_support_strength": best_level.strength
                if best_level
                else None,
                "recon_best_support_families": best_level.families
                if best_level
                else None,
                "recon_missing": missing,
                "target_kind": rej.target_kind,
                "direction": rej.direction,
                "target_price": rej.target_price,
                "probability": rej.probability,
                "status": rej.status,
                "outcome": rej.outcome,
                "scored": rej.status == "closed" and rej.outcome is not None,
                "episode_anchor": anchor,
            }
        )
    episodes = assign_episodes(episode_inputs, _sessions_between(store))
    df = pd.DataFrame(rows)
    df["parent_episode_id"] = df["row"].map(episodes)
    df["episode_rule"] = (
        f"same market+symbol; gap to previous record <= {EPISODE_SESSIONS_MAX} sessions "
        f"(crypto <= {EPISODE_CRYPTO_HOURS}h); anchor within ±{EPISODE_ANCHOR_TOL_PCT:g}% "
        "of episode's first anchor (anchor = recomputed closest-to-passing support, else target)"
    )
    return df
