"""Q2 (20/80 split anchor) and Q3 (3-session resubmission) on Toss BUY limits.

Population: ``toss_orders.json`` LIMIT BUYs -- unfilled (CANCELED/REJECTED with
0 filled; Toss reports a regular-session DAY expiry as REJECTED at 15:34 KST)
and FILLED (comparison group). Market BUYs (no limit) and PENDING are skipped.
Crypto has no orders in the inputs (Toss is KR/US only).

Order lifetime ``[ordered_at, canceled_at|filled_at]``. Order sessions = daily
bars whose regular session overlaps it. Lows only count trading after the
order: hourly bars that *start* at/after ``ordered_at`` (the hour containing
the order is excluded -- conservative) and end by the expiry; sessions fully
inside the lifetime use the daily low. KR bars are KRX regular session only
(NXT 15:30-20:00 and the KRX after-market are not in public daily/hourly
bars). US bars are the regular ET session.

Reference ("current price at order time") = open of the hourly bar containing
``ordered_at``; outside regular hours, the first regular hourly bar after it;
no hourly data -> the session's daily open (``reference_basis`` records which).

Budget = ``price x quantity`` of the actual order (the approved notional; the
proposal export confirms one rung per proposal).

Two fill scenarios bound the uncertainty (``core.leg_filled``):

* ``optimistic``   -- lows include the hour containing the order, and a print
  at the limit fills (``touch``: low <= limit).
* ``conservative`` -- lows start with the first hour beginning after the order,
  and price must trade one tick through the limit (``trade_through``:
  low <= limit - 1 tick), so the queue at the limit is certainly exhausted.

Q3 resubmission sessions are whole sessions, so only the fill rule differs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.core import (
    fill_legs,
    forward_return,
    leg_filled,
    legs_return,
    low_after,
    tick_floor,
)
from research.rob1383_buy_drought.inputs import TossOrder
from research.rob1383_buy_drought.prices import PriceStore
from research.rob1383_buy_drought.stats import mean_row, proportion_row, ratio_ci

RETURN_HORIZONS = (5, 20)
NEAR_LEG_PCT = 0.5  # 20% of budget at reference x (1 - 0.5%)
NEAR_LEG_WEIGHT = 0.2
RESUBMIT_SESSIONS = 3
QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
SCHEMES = ("actual_anchor_100", "split_20_ref-0.5pct_80_anchor")
SCENARIOS = {"optimistic": (True, "touch"), "conservative": (False, "trade_through")}


def expiry_bucket(market: str, expired_kst_hhmm: str | None) -> str | None:
    """KR: 15:3x = KRX regular-close expiry, 20:0x = NXT-close expiry; US 08:50."""

    if expired_kst_hhmm is None:
        return None
    hour = int(expired_kst_hhmm[:2])
    if market == "kr":
        return (
            "kr_1530_regular_close"
            if hour == 15
            else "kr_2000_nxt_close"
            if hour == 20
            else f"kr_{expired_kst_hhmm}"
        )
    return "us_0850_kst" if expired_kst_hhmm == "08:50" else f"us_{expired_kst_hhmm}"


def order_group(o: TossOrder) -> str | None:
    if o.side != "BUY" or o.price is None or o.status == "PENDING":
        return None
    if o.status == "FILLED":
        return "filled_comparison"
    if o.status in ("CANCELED", "REJECTED") and o.filled_qty == 0 and o.canceled_at:
        return "unfilled"
    return None


def order_session_positions(bars: pd.DataFrame, o: TossOrder) -> list[int]:
    end = pd.Timestamp(o.canceled_at or o.filled_at)
    start = pd.Timestamp(o.ordered_at)
    mask = (bars["open_ts"] < end) & (bars["close_ts"] > start)
    return [int(i) for i in np.flatnonzero(mask.to_numpy())]


def reference_price(
    bars: pd.DataFrame, hourly: pd.DataFrame | None, pos: int, ordered_at: Any
) -> tuple[float, str]:
    ts = pd.Timestamp(ordered_at)
    if hourly is not None:
        day = hourly[hourly["session_date"] == bars["session_date"].iloc[pos]]
        containing = day[(day["start_ts"] <= ts) & (day["end_ts"] > ts)]
        if len(containing):
            return float(containing["open"].iloc[0]), "hourly_containing"
        later = day[day["start_ts"] > ts]
        if len(later):
            return float(later["open"].iloc[0]), "hourly_first_after"
    return float(bars["open"].iloc[pos]), "daily_open"


def lifetime_low(
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    positions: list[int],
    o: TossOrder,
    *,
    inclusive: bool,
) -> tuple[float | None, str]:
    end = o.canceled_at or o.filled_at
    lows, bases = [], []
    for p in positions:
        low, basis = low_after(bars, hourly, p, o.ordered_at, end, inclusive=inclusive)
        bases.append(basis)
        if low is not None:
            lows.append(low)
    return (min(lows) if lows else None), "+".join(sorted(set(bases)))


def scheme_legs(
    o: TossOrder, reference: float, scheme: str
) -> list[tuple[float, float]]:
    if scheme == "actual_anchor_100":
        return [(o.price, 1.0)]
    near = tick_floor(o.market, reference * (1 - NEAR_LEG_PCT / 100.0))
    return [(near, NEAR_LEG_WEIGHT), (o.price, 1.0 - NEAR_LEG_WEIGHT)]


def _actual_rebuy(
    orders: list[TossOrder], o: TossOrder, bars: pd.DataFrame, nxt: list[int]
) -> bool:
    """Did the live system itself place another BUY of the symbol in those sessions?"""

    if not nxt:
        return False
    end = bars["close_ts"].iloc[nxt[-1]]
    return any(
        x.side == "BUY"
        and x.symbol == o.symbol
        and x.order_id != o.order_id
        and pd.Timestamp(o.canceled_at) <= pd.Timestamp(x.ordered_at) <= end
        for x in orders
    )


def _q2_record(
    o: TossOrder,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    pos: list[int],
    closes: Any,
) -> dict[str, Any]:
    """Lifetime lows, reference price and the two Q2 schemes x scenarios."""

    low, low_basis = lifetime_low(bars, hourly, pos, o, inclusive=False)
    low_incl, _ = lifetime_low(bars, hourly, pos, o, inclusive=True)
    reference, ref_basis = reference_price(bars, hourly, pos[0], o.ordered_at)
    day0 = pos[-1]
    rec: dict[str, Any] = {
        "session_date": str(bars["session_date"].iloc[pos[0]]),
        "n_order_sessions": len(pos),
        "lifetime_low": low,
        "lifetime_low_incl_order_hour": low_incl,
        "low_basis": low_basis,
        "reference_price": reference,
        "reference_basis": ref_basis,
        "anchor_vs_reference_pct": (o.price / reference - 1.0) * 100.0,
        "low_vs_anchor_pct": (low / o.price - 1.0) * 100.0 if low is not None else None,
        "low_incl_vs_anchor_pct": (
            (low_incl / o.price - 1.0) * 100.0 if low_incl is not None else None
        ),
    }
    for scheme in SCHEMES:
        legs = scheme_legs(o, reference, scheme)
        rec[f"{scheme}__near_limit"] = legs[0][0] if len(legs) > 1 else None
        for scenario, (inclusive, rule) in SCENARIOS.items():
            window_low = low_incl if inclusive else low
            fill = fill_legs(o.market, window_low, legs, rule)
            key = f"{scheme}__{scenario}"
            rec[f"{key}__any_fill"] = fill.any_fill
            rec[f"{key}__filled_weight"] = fill.filled_weight
            for h in RETURN_HORIZONS:
                pos_h = day0 + h
                rec[f"{key}__ret{h}"] = (
                    legs_return(float(closes[pos_h]), legs, fill.filled)
                    if fill.any_fill and pos_h < len(closes)
                    else None
                )
    return rec


def run(orders: Iterable[TossOrder], store: PriceStore) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    all_orders = list(orders)
    for o in all_orders:
        group = order_group(o)
        if group is None:
            continue
        base: dict[str, Any] = {
            "order_id": o.order_id,
            "market": o.market,
            "symbol": o.symbol,
            "group": group,
            "status": o.status,
            "price": o.price,
            "quantity": o.quantity,
            "budget": o.price * o.quantity,
            "ordered_at": o.ordered_at.isoformat(),
            "expired_at": o.canceled_at.isoformat() if o.canceled_at else None,
            "expiry_kst": o.canceled_at.strftime("%H:%M") if o.canceled_at else None,
            "expiry_bucket": expiry_bucket(
                o.market, o.canceled_at.strftime("%H:%M") if o.canceled_at else None
            ),
        }
        bars = store.bars(o.market, o.symbol)
        if bars is None:
            records.append(
                {**base, "missing": store.missing.get((o.market, o.symbol), "no_bars")}
            )
            continue
        split_dates = store.split_events.get((o.market, o.symbol), [])
        if any(d > str(o.ordered_at.date()) for d in split_dates):
            # yfinance split-adjusts: bars no longer comparable to the raw price.
            records.append({**base, "missing": "split_after_order"})
            continue
        pos = order_session_positions(bars, o)
        closes = bars["close"].to_numpy()
        opens = bars["open"].to_numpy()
        lows = bars["low"].to_numpy()
        rec: dict[str, Any] = {**base, "missing": None, "q2_missing": None}
        if not pos:
            # No KRX regular session overlapped the original (NXT-only) order:
            # Q2 has no lifetime prices, but Q3's later KRX sessions are
            # still observable, so the row stays in Q3 (tester r1 B3).
            rec["q2_missing"] = "no_regular_session_overlap"
        else:
            rec.update(
                _q2_record(o, bars, store.hourly(o.market, o.symbol), pos, closes)
            )
        # ---------------------------------------------------------------- Q3
        if group == "unfilled":
            after = pd.Timestamp(o.canceled_at)
            nxt = [
                int(i) for i in np.flatnonzero((bars["open_ts"] >= after).to_numpy())
            ]
            nxt = nxt[:RESUBMIT_SESSIONS]
            rec["q3_sessions_observed"] = len(nxt)
            rec["q3_actual_rebuy_within_3"] = _actual_rebuy(all_orders, o, bars, nxt)
            for scenario, (_inclusive, rule) in SCENARIOS.items():
                key = f"q3__{scenario}"
                k_fill = next(
                    (
                        k
                        for k, p in enumerate(nxt, start=1)
                        if leg_filled(o.market, float(lows[p]), o.price, rule)
                    ),
                    None,
                )
                if k_fill is None:
                    rec[f"{key}__status"] = (
                        "not_filled" if len(nxt) == RESUBMIT_SESSIONS else "censored"
                    )
                    continue
                p = nxt[k_fill - 1]
                gap_down = float(opens[p]) <= o.price
                fill_price = float(opens[p]) if gap_down else o.price
                rec[f"{key}__status"] = "filled"
                rec[f"{key}__sessions_to_fill"] = k_fill
                rec[f"{key}__gap_down_fill"] = gap_down
                rec[f"{key}__fill_price"] = fill_price
                rec[f"{key}__open_vs_anchor_pct"] = (
                    float(opens[p]) / o.price - 1.0
                ) * 100.0
                rec[f"{key}__close_vs_fill_pct"] = (
                    float(closes[p]) / fill_price - 1.0
                ) * 100.0
                for h in RETURN_HORIZONS:
                    rec[f"{key}__ret{h}"] = forward_return(closes, p, h, fill_price)
        records.append(rec)
    return pd.DataFrame.from_records(records)


# ------------------------------------------------------------------ summaries


def _quantiles(values: pd.Series, prefix: str) -> dict[str, float]:
    v = values.dropna().astype(float)
    out = {
        f"{prefix}_q{int(q * 100):02d}": (float(v.quantile(q)) if len(v) else np.nan)
        for q in QUANTILES
    }
    out.update(mean_row(v.tolist(), prefix))
    return out


def _col(frame: pd.DataFrame, name: str, dtype: str = "float") -> pd.Series:
    return frame[name] if name in frame else pd.Series(dtype=dtype, index=frame.index)


def _q2_scorable(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["missing"].isna() & frame["q2_missing"].isna()]


def summarise_q2(detail: pd.DataFrame, market: str) -> pd.DataFrame:
    m = detail[detail["market"] == market]
    rows = []
    for group in ("unfilled", "filled_comparison"):
        g_all = m[m["group"] == group]
        g = _q2_scorable(g_all)
        for scheme in SCHEMES:
            for rule in SCENARIOS:
                key = f"{scheme}__{rule}"
                flags = _col(g, f"{key}__any_fill", "bool").fillna(False).astype(bool)
                fill = proportion_row(flags.tolist(), g["symbol"].tolist())
                weight = _col(g, f"{key}__filled_weight").fillna(0.0).to_numpy(float)
                budget = g["budget"].to_numpy(float)
                filled_amt = weight * budget
                ratio = (
                    float(filled_amt.sum() / budget.sum()) if budget.sum() else np.nan
                )
                lo, hi = ratio_ci(filled_amt, budget)
                row = {
                    "market": market,
                    "group": group,
                    "scheme": scheme,
                    "scenario": rule,
                    "n_orders": len(g_all),
                    "n_scored": fill["n"],
                    "n_excluded": len(g_all) - len(g),
                    "n_qty_lt_5_split_not_whole_shares": (
                        int((g["quantity"] < 5).sum()) if scheme != SCHEMES[0] else 0
                    ),
                    "fill_k": fill["k"],
                    "fill_rate": fill["rate"],
                    "fill_wilson_lo": fill["wilson_lo"],
                    "fill_wilson_hi": fill["wilson_hi"],
                    "fill_cluster_lo": fill.get("cluster_lo"),
                    "fill_cluster_hi": fill.get("cluster_hi"),
                    "budget_total": float(budget.sum()),
                    "filled_notional_ratio": ratio,
                    "filled_notional_ratio_lo": lo,
                    "filled_notional_ratio_hi": hi,
                }
                for h in RETURN_HORIZONS:
                    row.update(
                        mean_row(_col(g, f"{key}__ret{h}").dropna().tolist(), f"ret{h}")
                    )
                rows.append(row)
    return pd.DataFrame(rows)


def q2_distribution(detail: pd.DataFrame, market: str) -> pd.DataFrame:
    g = _q2_scorable(detail[detail["market"] == market])
    rows = []
    for group in ("unfilled", "filled_comparison"):
        x = g[g["group"] == group]
        for metric in (
            "low_vs_anchor_pct",
            "low_incl_vs_anchor_pct",
            "anchor_vs_reference_pct",
        ):
            rows.append(
                {
                    "market": market,
                    "group": group,
                    "metric": metric,
                    "n": int(_col(x, metric).notna().sum()),
                    **_quantiles(_col(x, metric), "v"),
                }
            )
    return pd.DataFrame(rows)


def summarise_q3(detail: pd.DataFrame, market: str) -> pd.DataFrame:
    g_all = detail[(detail["market"] == market) & (detail["group"] == "unfilled")]
    g = g_all[g_all["missing"].isna()]
    rows = []
    for expiry in ["all", *sorted(g["expiry_bucket"].dropna().unique())]:
        e = g if expiry == "all" else g[g["expiry_bucket"] == expiry]
        for rule in SCENARIOS:
            key = f"q3__{rule}"
            status = _col(e, f"{key}__status", "object")
            observed = e[status.isin(["filled", "not_filled"])]
            filled = e[status == "filled"]
            p = proportion_row(
                (status[observed.index] == "filled").tolist(),
                observed["symbol"].tolist(),
            )
            stf = _col(filled, f"{key}__sessions_to_fill")
            gap = filled[
                _col(filled, f"{key}__gap_down_fill", "bool").fillna(False).astype(bool)
            ]
            row = {
                "market": market,
                "expiry_bucket": expiry,
                "scenario": rule,
                "resubmit_sessions": RESUBMIT_SESSIONS,
                "n_unfilled": len(g_all) if expiry == "all" else len(e),
                "n_excluded": (len(g_all) - len(g)) if expiry == "all" else 0,
                "n_censored": int((status == "censored").sum()),
                "n_observed": p["n"],
                "extra_fill_k": p["k"],
                "extra_fill_rate": p["rate"],
                "extra_fill_wilson_lo": p["wilson_lo"],
                "extra_fill_wilson_hi": p["wilson_hi"],
                "extra_fill_cluster_lo": p.get("cluster_lo"),
                "extra_fill_cluster_hi": p.get("cluster_hi"),
                "fill_in_session_1": int((stf == 1).sum()),
                "fill_in_session_2": int((stf == 2).sum()),
                "fill_in_session_3": int((stf == 3).sum()),
                "n_actual_rebuy_within_3": int(
                    _col(e, "q3_actual_rebuy_within_3", "bool")
                    .fillna(False)
                    .astype(bool)
                    .sum()
                ),
                "gap_down_fill_k": len(gap),
                **_quantiles(
                    _col(gap, f"{key}__open_vs_anchor_pct"),
                    "gapfill_open_vs_anchor_pct",
                ),
                **mean_row(
                    _col(gap, f"{key}__close_vs_fill_pct").dropna().tolist(),
                    "gapfill_close_vs_open_pct",
                ),
            }
            for h in RETURN_HORIZONS:
                row.update(
                    mean_row(
                        _col(filled, f"{key}__ret{h}").dropna().tolist(), f"ret{h}"
                    )
                )
            rows.append(row)
    return pd.DataFrame(rows)


def coverage(detail: pd.DataFrame) -> pd.DataFrame:
    """Disposition of every in-scope order (no silent drops)."""

    d = detail.copy()
    d["disposition"] = (
        d["missing"]
        .fillna(
            d["q2_missing"].map(
                lambda v: f"q3_only:q2_{v}" if isinstance(v, str) else None
            )
        )
        .fillna("scored")
    )
    out = d.groupby(["market", "group", "disposition"]).size().reset_index(name="n")
    s = _q2_scorable(d)
    extra = []
    for col in ("low_basis", "reference_basis"):
        x = s.groupby(["market", "group", col]).size().reset_index(name="n")
        x["disposition"] = col + ":" + x.pop(col).astype(str)
        extra.append(x)
    return pd.concat([out, *extra], ignore_index=True)
