"""v1 reproduction + the operator's canonical figures, recomputed row by row.

1. Re-run v1 ``q2_q3_orders.run`` (imported, not copied) on the frozen
   inputs and price cache, and diff it against the v1 ``q2_q3_detail.csv``
   the v1 job wrote (byte-level reproduction check of every shared column).
2. Recompute each canonical number (37/63, 63->47->42, 72->23->11, NXT 16,
   actual re-buys, parent units, Q4 upside-only) from that detail and from
   v1 ``q4_kr.csv``, and return ``(item, canonical, recomputed, match, note)``.
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from research.rob1383_buy_drought import q2_q3_orders as q23
from research.rob1383_buy_drought.inputs import TossOrder
from research.rob1383_buy_drought.prices import PriceStore
from research.rob1404_kr_entry.population import overlaps_regular_window

SPLIT = "split_20_ref-0.5pct_80_anchor"


def rerun_v1_detail(orders: list[TossOrder], store: PriceStore) -> pd.DataFrame:
    return q23.run(orders, store)


def diff_against_saved(fresh: pd.DataFrame, saved_csv: Path) -> dict[str, object]:
    """Round-trip the fresh v1 detail through v1's own CSV writer settings and
    compare cell by cell (as text) with the CSV the v1 job saved."""

    buf = io.StringIO()
    fresh.to_csv(buf, index=False, float_format="%.6g")
    buf.seek(0)
    a = pd.read_csv(saved_csv, dtype=str, keep_default_na=False).set_index("order_id")
    b = pd.read_csv(buf, dtype=str, keep_default_na=False).set_index("order_id")
    a, b = a.sort_index(), b.sort_index()
    if list(a.index) != list(b.index):
        return {"rows_saved": len(a), "rows_fresh": len(b), "index_equal": False}
    cols = [c for c in a.columns if c in b.columns]
    mismatched = [c for c in cols if not (a[c] == b[c]).all()]
    return {
        "rows_saved": len(a),
        "rows_fresh": len(b),
        "index_equal": True,
        "columns_saved": len(a.columns),
        "columns_fresh": len(b.columns),
        "columns_compared": len(cols),
        "cells_compared": len(cols) * len(a),
        "columns_mismatched": "|".join(mismatched) or "none",
    }


def _row(item: str, canonical: object, recomputed: object, note: str = "") -> dict:
    match = (
        "n/a"
        if canonical is None
        else ("match" if str(canonical) == str(recomputed) else "MISMATCH")
    )
    return {
        "item": item,
        "canonical": canonical,
        "recomputed": recomputed,
        "match": match,
        "note": note,
    }


def canonical_rows(detail: pd.DataFrame, q4_kr_csv: Path) -> list[dict]:
    kr = detail[(detail["market"] == "kr") & (detail["group"] == "unfilled")]
    scored = kr[kr["missing"].isna() & kr["q2_missing"].isna()]
    nxt = kr[kr["q2_missing"] == "no_regular_session_overlap"]
    q = scored["quantity"].astype(float)
    nxt_time = pd.Series(
        [
            not overlaps_regular_window(
                datetime.fromisoformat(a), datetime.fromisoformat(b)
            )
            for a, b in zip(nxt["ordered_at"], nxt["expired_at"], strict=True)
        ],
        index=nxt.index,
        dtype=bool,
    )
    rows = [
        _row("KR unfilled LIMIT BUY (v1 Q2/Q3 population)", 79, len(kr)),
        _row("Q2 scored (regular-session overlap)", 63, len(scored)),
        _row("NXT-only (no KRX regular overlap; Q2 excluded, Q3 kept)", 16, len(nxt)),
        _row(
            "  of which genuine NXT-time orders (placed >= 15:30 KST, expiry 20:0x)",
            None,
            int(nxt_time.sum()),
            "lifetime outside 09:00-15:30 KST by timestamps",
        ),
        _row(
            "  of which regular-session orders whose day has no cached daily bar",
            None,
            int((~nxt_time).sum()),
            "2026-10-16 orders; v1 daily cache ends 2026-10-15 -> Q3 censored, Q2 no prices",
        ),
        _row(
            "Q2 qty < 5 (20% leg below one whole share) — canonical '37/63'",
            37,
            int((q < 5).sum()),
            "v1 column n_qty_lt_5_split_not_whole_shares",
        ),
        _row(
            "Q2 qty not divisible by 5 (no exact 20/80 whole-share split by count)",
            None,
            int((q % 5 != 0).sum()),
            "stricter reading of 'exact 20/80 integer split impossible'",
        ),
        _row(
            "Q2 orders whose floor(0.2 q) near leg exists (q >= 5)",
            None,
            int((q >= 5).sum()),
        ),
    ]
    for scen, fills, matured in (("optimistic", 47, 42), ("conservative", None, None)):
        key = f"{SPLIT}__{scen}"
        any_fill = scored[f"{key}__any_fill"].fillna(False).astype(bool)
        ret5 = scored[f"{key}__ret5"]
        ret20 = scored[f"{key}__ret20"]
        rows += [
            _row(
                f"Q2 −0.5% split {scen}: virtual fills / 63", fills, int(any_fill.sum())
            ),
            _row(
                f"Q2 −0.5% split {scen}: matured day+5 obs",
                matured,
                int(ret5.notna().sum()),
                "day 0 = last lifetime session (q2_q3_orders.py _q2_record: day0 = pos[-1])",
            ),
            _row(
                f"Q2 −0.5% split {scen}: matured day+20 obs",
                None,
                int(ret20.notna().sum()),
            ),
            _row(
                f"Q2 −0.5% split {scen}: mean day+5 return % (pre-cost)",
                None,
                round(float(ret5.mean()), 3) if ret5.notna().any() else math.nan,
                "legs_return(): no commission/tax/slippage term",
            ),
        ]
    for scen, obs, fills, matured in (
        ("optimistic", 72, 23, 11),
        ("conservative", None, None, None),
    ):
        st = kr[f"q3__{scen}__status"]
        observed = st.isin(["filled", "not_filled"])
        filled = st == "filled"
        r5 = kr[f"q3__{scen}__ret5"]
        rows += [
            _row(
                f"Q3 resubmit {scen}: observed (3 sessions seen)",
                obs,
                int(observed.sum()),
            ),
            _row(f"Q3 resubmit {scen}: censored", None, int((st == "censored").sum())),
            _row(f"Q3 resubmit {scen}: extra virtual fills", fills, int(filled.sum())),
            _row(
                f"Q3 resubmit {scen}: matured day+5 obs",
                matured,
                int(r5.notna().sum()),
                "day 0 = virtual fill session (forward_return(closes, p, h, fill_price))",
            ),
            _row(
                f"Q3 resubmit {scen}: mean day+5 return % (pre-cost)",
                None,
                round(float(r5.mean()), 3) if r5.notna().any() else math.nan,
            ),
            _row(
                f"Q3 resubmit {scen}: fills among NXT-only originals",
                None,
                int((filled & kr["q2_missing"].eq("no_regular_session_overlap")).sum()),
            ),
        ]
    rebuy = kr["q3_actual_rebuy_within_3"].fillna(False).astype(bool)
    obs_mask = kr["q3__optimistic__status"].isin(["filled", "not_filled"])
    rows.append(
        _row(
            "Q3: live system re-placed the same symbol within the 3 sessions",
            "37/72",
            f"{int((rebuy & obs_mask).sum())}/{int(obs_mask.sum())}",
        )
    )
    q4 = pd.read_csv(q4_kr_csv)
    sel = q4[(q4["level"] == "episode") & (q4["measure"] == "excess_touch")]
    for direction, canonical in (
        ("all", "+16.1"),
        ("at_or_above", None),
        ("at_or_below", None),
    ):
        r = sel[sel["direction"] == direction].iloc[0]
        rows.append(
            _row(
                f"Q4 excess_touch gap pp ({direction}), episodes n={int(r['n'])}",
                canonical,
                f"{r['gap_hit_minus_prob'] * 100:+.1f}",
                f"symbol-cluster 95% CI {r['gap_cluster_lo'] * 100:+.1f}..{r['gap_cluster_hi'] * 100:+.1f}pp; "
                f"hit {r['hit_rate'] * 100:.1f}% vs stated prob {r['mean_probability'] * 100:.1f}% — a hit-rate gap, not a return",
            )
        )
    end = q4[(q4["level"] == "episode") & (q4["measure"] == "excess_end")]
    r = end[end["direction"] == "at_or_above"].iloc[0]
    rows.append(
        _row(
            "Q4 excess_end gap pp (at_or_above), episodes",
            None,
            f"{r['gap_hit_minus_prob'] * 100:+.1f}",
            f"symbol-cluster 95% CI {r['gap_cluster_lo'] * 100:+.1f}..{r['gap_cluster_hi'] * 100:+.1f}pp",
        )
    )
    return rows
