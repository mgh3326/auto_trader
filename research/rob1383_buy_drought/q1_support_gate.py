"""Q1 (rev2) -- support-gate relaxation over ``support_only_fail`` candidates.

Point-in-time support is rebuilt by the *live* ``get_support_resistance_impl``
(preloaded 60-bar frame ending at the decision instant) and RSI by the live
``_compute_indicators`` -- both reached through the ROB-1301 backtest shim
``research.buy_gate_ab_backtest.reconstruct``, which pins inert settings and
disables the US live-quote fetch. Nothing in the level logic is reimplemented.

Gate authority: ``config/trading_policy.yaml`` version 2026-09-30.1 (content
hash 62c7a733a778, identical to the input snapshot):
``screen.support_strength_min`` moderate, ``screen.independent_support_source_count_min``
2, ``screen.support_within_pct`` 8, ``buy.deep_limit_pct_range`` [-12, -3].
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

# Import order matters: the shim pins inert env + disables live quotes first.
from research.buy_gate_ab_backtest import reconstruct as _shim
from research.rob1383_buy_drought.core import (
    VARIANT_NAMES,
    GateVariant,
    Reconstruction,
    SupportLevel,
    build_variant,
    evaluate_support_gate,
    first_touch,
    forward_positions,
    forward_return,
    low_after,
    point_in_time,
)
from research.rob1383_buy_drought.gates import RSI_MAX, text_states_support_pass
from research.rob1383_buy_drought.inputs import Rejection
from research.rob1383_buy_drought.prices import PriceStore
from research.rob1383_buy_drought.stats import mean_row, proportion_row

VARIANTS: tuple[GateVariant, ...] = tuple(build_variant(n) for n in VARIANT_NAMES)

TOUCH_WINDOWS = (1, 5)  # DAY expiry = 1 session; +5 sessions
RETURN_HORIZONS = (5, 20)
RSI_WINDOW = 250
SR_WINDOW = 60

POPULATIONS = {
    "support_only_fail": lambda e: e["support_only_fail"],
    "support_only_fail_text_basis": lambda e: e["support_only_basis"] == "text",
    "deep_limit_shallow_text": lambda e: (
        e["support_only_fail"]
        & e["failed_gates_text"].fillna("").str.contains("deep_limit_band")
    ),
    "deep_limit_shallow_recomputed_only": lambda e: (
        e["support_only_fail"]
        & (e["failed_gates_recomputed"].fillna("") == "deep_limit_band_shallow")
    ),
}


def reconstruct(
    rej: Rejection,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    *,
    price_mode: str = "point_in_time",
) -> Reconstruction | str:
    """Rebuild what the live S/R tool returned at ``created_at``.

    ``price_mode="point_in_time"`` appends the in-progress partial bar (see
    ``core.point_in_time``); ``"prior_close"`` uses completed bars only.
    """

    pit = point_in_time(
        bars, hourly if price_mode == "point_in_time" else None, rej.created_at
    )
    if pit is None or len(pit.history) < SR_WINDOW:
        return "insufficient_history"
    hist = pit.history
    window_rsi = hist.iloc[-RSI_WINDOW:].reset_index(drop=True)
    window_sr = hist.iloc[-SR_WINDOW:].reset_index(drop=True)
    try:
        sr = _shim._Loop.run(
            _shim._sr.get_support_resistance_impl(
                _shim._sr_symbol_arg(rej.market, rej.symbol),
                market=_shim._sr_market_arg(rej.market),
                preloaded_df=window_sr,
            )
        )
    except ValueError:
        return "symbol_not_resolvable_by_live_router"
    if not isinstance(sr, dict) or "error" in sr:
        return "support_resistance_error"
    current = float(sr["current_price"])
    if current <= 0:
        return "price_rounds_to_zero"
    rsi = (_shim._compute_indicators(window_rsi, ["rsi"]).get("rsi") or {}).get("14")
    supports = tuple(
        SupportLevel(
            price=float(level["price"]),
            strength=str(level.get("strength") or "").lower(),
            families=len(
                {
                    fam
                    for src in level.get("sources") or []
                    if (fam := _shim._support_family(str(src))) is not None
                }
            ),
            sources=tuple(str(s) for s in level.get("sources") or []),
        )
        for level in sr.get("supports") or []
        if isinstance(level, dict) and level.get("price")
    )
    basis = pit.price_basis if price_mode == "point_in_time" else "prior_close_mode"
    return Reconstruction(
        current, float(rsi) if rsi is not None else None, supports, basis
    )


def reconstruct_all(
    rejections: Iterable[Rejection],
    store: PriceStore,
    *,
    price_mode: str = "point_in_time",
    reuse: dict[int, Reconstruction | str] | None = None,
) -> dict[int, Reconstruction | str]:
    """Reconstruct every row. ``reuse`` (point-in-time results) short-cuts the
    prior-close pass: when no partial bar was appended the two inputs are the
    same completed-bar frame, so the result is identical by construction."""

    out: dict[int, Reconstruction | str] = {}
    for rej in rejections:
        prev = (reuse or {}).get(rej.row)
        if (
            price_mode == "prior_close"
            and isinstance(prev, Reconstruction)
            and prev.price_basis != "intraday_partial"
        ):
            out[rej.row] = Reconstruction(
                prev.current_price, prev.rsi, prev.supports, "prior_close_mode"
            )
            continue
        bars = store.bars(rej.market, rej.symbol)
        if bars is None:
            out[rej.row] = store.missing.get((rej.market, rej.symbol), "no_bars")
            continue
        out[rej.row] = reconstruct(
            rej, bars, store.hourly(rej.market, rej.symbol), price_mode=price_mode
        )
    return out


def forward_lows(
    bars: pd.DataFrame, hourly: pd.DataFrame | None, at: Any, n: int
) -> tuple[list[int], list[float | None], str]:
    """Daily positions of the first ``n`` sessions after ``at`` and their lows.

    The first session's low only counts trading after ``at`` (hourly, the hour
    containing ``at`` excluded); later sessions use the full daily low.
    """

    fwd = forward_positions(bars, at, n)
    lows: list[float | None] = []
    basis = "none"
    for i, p in enumerate(fwd):
        if i == 0:
            low, basis = low_after(bars, hourly, p, at)
            lows.append(low)
        else:
            lows.append(float(bars["low"].iloc[p]))
    return fwd, lows, basis


def touch_outcome(
    closes: Any,
    forward: tuple[list[int], list[float | None], str],
    anchor: float,
) -> dict[str, Any]:
    """Touch status and post-touch returns for one anchor.

    ``forward`` = ``forward_lows(...)`` for the row, computed once and shared
    by every variant's anchor.
    """

    out: dict[str, Any] = {}
    fwd_all, lows_all, basis = forward
    out["first_session_low_basis"] = basis
    for n in TOUCH_WINDOWS:
        window = fwd_all[:n]
        offset = first_touch(lows_all[:n], anchor)
        if offset is not None:
            status = "touched"
        elif len(window) < n:
            status = "censored"
        else:
            status = "not_touched"
        out[f"touch_n{n}"] = status
        for h in RETURN_HORIZONS:
            out[f"ret{h}_n{n}"] = (
                forward_return(closes, window[offset], h, anchor)
                if offset is not None
                else None
            )
    return out


def run(
    rejections: Iterable[Rejection],
    store: PriceStore,
    recon: dict[int, Reconstruction | str],
    enriched: pd.DataFrame,
) -> pd.DataFrame:
    """One row per (reconstructed rejection, variant) with population flags."""

    flags = enriched.set_index("row")
    pops = {
        name: fn(enriched).set_axis(enriched["row"]) for name, fn in POPULATIONS.items()
    }
    records: list[dict[str, Any]] = []
    for rej in rejections:
        rc = recon[rej.row]
        base: dict[str, Any] = {
            "row": rej.row,
            "market": rej.market,
            "symbol": rej.symbol,
            "created_at_utc": rej.created_at.isoformat(),
            "parent_episode_id": flags.at[rej.row, "parent_episode_id"],
            **{f"pop_{k}": bool(v.get(rej.row, False)) for k, v in pops.items()},
        }
        if isinstance(rc, str):
            records.append({**base, "missing": rc})
            continue
        bars = store.bars(rej.market, rej.symbol)
        hourly = store.hourly(rej.market, rej.symbol)
        closes = bars["close"].to_numpy()
        forward = forward_lows(bars, hourly, rej.created_at, max(TOUCH_WINDOWS))
        base.update(
            missing=None, current_price=rc.current_price, price_basis=rc.price_basis
        )
        for variant in VARIANTS:
            gate = evaluate_support_gate(
                rej.market, rc.supports, rc.current_price, variant
            )
            row = {**base, "variant": variant.name, "pass_support": gate.passed}
            if gate.passed and gate.level is not None and gate.anchor is not None:
                row.update(
                    support_price=gate.level.price,
                    support_strength=gate.level.strength,
                    support_families=gate.level.families,
                    support_sources="|".join(gate.level.sources),
                    anchor=gate.anchor,
                    anchor_distance_pct=gate.anchor_distance_pct,
                    **touch_outcome(closes, forward, gate.anchor),
                )
            records.append(row)
    return pd.DataFrame.from_records(records)


def summarise(detail: pd.DataFrame, market: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    m_all = detail[detail["market"] == market]
    for pop in POPULATIONS:
        members = m_all[m_all[f"pop_{pop}"].astype(bool)]
        n_pop = members["row"].nunique()
        ok = members[members["missing"].isna()]
        n_recon = ok["row"].nunique()
        for variant in VARIANT_NAMES:
            v = ok[ok["variant"] == variant]
            p = proportion_row(
                v["pass_support"].astype(bool).tolist(), v["symbol"].tolist()
            )
            sel = v[v["pass_support"].astype(bool)]
            for n in TOUCH_WINDOWS:
                col = f"touch_n{n}" if f"touch_n{n}" in sel else None
                status = sel[col] if col else pd.Series(dtype=object)
                ev = (
                    sel[status.isin(["touched", "not_touched"])]
                    if col
                    else sel.iloc[0:0]
                )
                t = proportion_row(
                    (ev[f"touch_n{n}"] == "touched").tolist() if len(ev) else [],
                    ev["symbol"].tolist() if len(ev) else [],
                )
                row = {
                    "market": market,
                    "population": pop,
                    "variant": variant,
                    "n_population": n_pop,
                    "n_reconstructed": n_recon,
                    "n_missing_prices_or_history": n_pop - n_recon,
                    "n_pass": p["k"],
                    "pass_rate": p["rate"],
                    "pass_wilson_lo": p["wilson_lo"],
                    "pass_wilson_hi": p["wilson_hi"],
                    "pass_n_symbols": sel["symbol"].nunique(),
                    "pass_n_episodes": sel["parent_episode_id"].nunique(),
                    "touch_window_sessions": n,
                    "touch_n_eval": t["n"],
                    "touch_n_censored": int((status == "censored").sum()) if col else 0,
                    "touch_k": t["k"],
                    "touch_rate": t["rate"],
                    "touch_wilson_lo": t["wilson_lo"],
                    "touch_wilson_hi": t["wilson_hi"],
                    "touch_cluster_lo": t.get("cluster_lo"),
                    "touch_cluster_hi": t.get("cluster_hi"),
                }
                for h in RETURN_HORIZONS:
                    vals = (
                        sel[f"ret{h}_n{n}"].dropna().tolist()
                        if f"ret{h}_n{n}" in sel
                        else []
                    )
                    row.update(mean_row(vals, f"ret{h}"))
                rows.append(row)
    return pd.DataFrame(rows)


def agreement(
    enriched: pd.DataFrame, rejections: Iterable[Rejection], recon_prior: dict[int, Any]
) -> pd.DataFrame:
    """Text-stated vs recomputed support verdicts, for both price modes."""

    from research.rob1383_buy_drought.core import recomputed_support_failures

    reasons = {r.row: r.reason for r in rejections}
    markets = {r.row: r.market for r in rejections}
    baseline = build_variant("baseline")
    e = enriched[enriched["recon_missing"].isna()].copy()
    e["text_support_fail"] = (
        e["failed_gates_text"]
        .fillna("")
        .str.contains(
            "support_strength|independent_sources|support_within_pct|deep_limit_band"
        )
    )
    e["text_support_pass"] = e["row"].map(
        lambda r: text_states_support_pass(reasons[r])
    )
    e["rec_fail_pit"] = (
        e["failed_gates_recomputed"]
        .fillna("")
        .str.contains("support|independent|deep_limit|no_support")
    )

    def prior_fail(row: int) -> bool | None:
        rc = recon_prior.get(row)
        if not isinstance(rc, Reconstruction):
            return None
        fails, _ = recomputed_support_failures(
            markets[row], rc.supports, rc.current_price, baseline
        )
        return bool(fails)

    e["rec_fail_prior"] = e["row"].map(prior_fail)
    e["recon_rsi_prior"] = e["row"].map(
        lambda r: (
            recon_prior[r].rsi
            if isinstance(recon_prior.get(r), Reconstruction)
            else None
        )
    )
    rows = []
    for market in ("kr", "us", "crypto"):
        m = e[e["market"] == market]
        for mode, col in (
            ("point_in_time", "rec_fail_pit"),
            ("prior_close", "rec_fail_prior"),
        ):
            f = m[m["text_support_fail"] & m[col].notna()]
            p = m[m["text_support_pass"] & m[col].notna()]
            a = proportion_row(f[col].astype(bool).tolist(), f["symbol"].tolist())
            b = proportion_row((~p[col].astype(bool)).tolist(), p["symbol"].tolist())
            rsi_col = "recon_rsi" if mode == "point_in_time" else "recon_rsi_prior"
            rsi = m.dropna(subset=[rsi_col])
            rsi = rsi[rsi["rsi_source"] == "text"]
            diff = (rsi[rsi_col] - rsi["rsi_value"]).abs()
            w = proportion_row((diff <= 3.0).tolist(), rsi["symbol"].tolist())
            rows.append(
                {
                    "market": market,
                    "price_mode": mode,
                    "n_reconstructed": len(m),
                    "n_basis_intraday_partial": int(
                        (m["recon_price_basis"] == "intraday_partial").sum()
                    ),
                    "n_basis_prior_close_no_hourly": int(
                        (m["recon_price_basis"] == "prior_close_no_hourly").sum()
                    ),
                    "text_states_support_fail_n": a["n"],
                    "recomputed_also_fail_k": a["k"],
                    "recall_text_fail": a["rate"],
                    "recall_wilson_lo": a["wilson_lo"],
                    "recall_wilson_hi": a["wilson_hi"],
                    "text_states_support_pass_n": b["n"],
                    "recomputed_also_pass_k": b["k"],
                    "agreement_text_pass": b["rate"],
                    "agree_pass_wilson_lo": b["wilson_lo"],
                    "agree_pass_wilson_hi": b["wilson_hi"],
                    "rsi_compared_n": w["n"],
                    "rsi_abs_diff_median": float(diff.median())
                    if len(diff)
                    else float("nan"),
                    "rsi_within_3pt_rate": w["rate"],
                    "rsi_within_3pt_wilson_lo": w["wilson_lo"],
                    "rsi_within_3pt_wilson_hi": w["wilson_hi"],
                    "rsi_threshold": RSI_MAX,
                }
            )
    return pd.DataFrame(rows)
