"""Buy funnel (rev2 §2): candidates -> proposed -> approved -> gate pass ->
broker accepted -> filled, plus filled amount / approved budget.

``approval_events`` had 0 rows in the window, so stages are reconstructed from
the proposal/rung state columns of ``proposals_buy.csv`` (one rung per
proposal; 231 records -- the file has 243 physical lines because quoted
``strategy`` cells contain newlines):

* proposed  : ``action == 'place'`` (the 3 ``cancel`` proposals are excluded)
* approved  : ``auto_approved`` starts with ``yes`` OR ``approved_at`` is set
* gate pass : approved AND ``broker_order_id`` set (submit-time revalidation
  gates passed and an order was sent)
* accepted  : gate pass AND the broker id matched the ledger
  (``ledger_status`` or ``toss_broker_status`` set); a sent id with neither is
  ``unmatched_ledger`` and is reported separately, never counted as accepted
* filled    : accepted AND (``rung_state == 'filled'`` OR ``ledger_filled_qty``
  > 0 OR ``toss_broker_status == 'FILLED'``)

Candidates are forecasts, not proposals: ``deferred_no_action`` +
``new_buy_candidate`` records created in the same window. The candidates ->
proposed ratio therefore compares two entities (proposals also include add and
reserve tiers) and is not a subset conversion.

Amounts are native currency (KRW for KR and crypto, USD for US). Budget per
approved rung = ``notional`` if set, else ``quantity x limit_price``; filled
amount = filled quantity x average fill price (limit price when the ledger has
no average).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.inputs import MARKET_OF_INSTRUMENT
from research.rob1383_buy_drought.stats import proportion_row, ratio_ci

STAGES = ("proposed", "approved", "gate_pass", "accepted", "filled")


def load_proposals(inputs: Path) -> pd.DataFrame | None:
    path = inputs / "proposals_buy.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["mkt"] = df["market"].map(MARKET_OF_INSTRUMENT)
    return df


def classify(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["proposed"] = d["action"] == "place"
    auto = d["auto_approved"].fillna("").astype(str).str.startswith("yes")
    d["approved"] = d["proposed"] & (auto | d["approved_at"].notna())
    d["gate_pass"] = d["approved"] & d["broker_order_id"].notna()
    matched = d["ledger_status"].notna() | d["toss_broker_status"].notna()
    d["unmatched_ledger"] = d["gate_pass"] & ~matched
    d["accepted"] = d["gate_pass"] & matched
    filled_qty = d["ledger_filled_qty"].fillna(d["rung_filled_qty"]).fillna(0.0)
    d["filled"] = d["accepted"] & (
        (d["rung_state"] == "filled")
        | (filled_qty > 0)
        | (d["toss_broker_status"] == "FILLED")
    )
    price = d["limit_price"].astype(float)
    d["budget_missing"] = d["notional"].isna() & (d["quantity"].isna() | price.isna())
    d["budget"] = d["notional"].fillna(d["quantity"].astype(float) * price).fillna(0.0)
    d["filled_amount"] = np.where(
        d["filled"], filled_qty * d["ledger_avg_fill_price"].fillna(price), 0.0
    )
    return d


def _candidates(inputs: Path, start: datetime, end: datetime) -> pd.DataFrame:
    payload = json.loads((inputs / "forecasts_all.json").read_text())["result"]
    if isinstance(payload, dict):
        payload = payload["entries"]
    rows = []
    for e in payload:
        bucket = e.get("decision_bucket")
        if bucket not in ("deferred_no_action", "new_buy_candidate"):
            continue
        ts = datetime.fromisoformat(e["created_at"])
        if start <= ts <= end:
            rows.append(
                {"mkt": MARKET_OF_INSTRUMENT[e["instrument_type"]], "bucket": bucket}
            )
    return pd.DataFrame(rows)


def summarise(inputs: Path) -> dict[str, pd.DataFrame]:
    proposals = load_proposals(inputs)
    if proposals is None:
        return {
            m: pd.DataFrame(
                [{"market": m, "stage": s, "status": "pending_data"} for s in STAGES]
            )
            for m in ("kr", "us", "crypto")
        }
    d = classify(proposals)
    start = datetime.fromisoformat(d["created_at"].min())
    end = datetime.fromisoformat(d["created_at"].max())
    cands = _candidates(inputs, start, end)
    out: dict[str, pd.DataFrame] = {}
    for market in ("kr", "us", "crypto"):
        m = d[d["mkt"] == market]
        c = cands[cands["mkt"] == market] if len(cands) else cands
        rows: list[dict[str, Any]] = [
            {
                "market": market,
                "stage": "candidates_forecasts",
                "n": len(c),
                "detail": (
                    f"deferred_no_action={int((c['bucket'] == 'deferred_no_action').sum())}; "
                    f"new_buy_candidate={int((c['bucket'] == 'new_buy_candidate').sum())}; "
                    f"window {start:%Y-%m-%d}..{end:%Y-%m-%d}"
                ),
            }
        ]
        prev = None
        for stage in STAGES:
            members = m[m[stage]]
            row: dict[str, Any] = {"market": market, "stage": stage, "n": len(members)}
            if prev is not None:
                base = m[m[prev]]
                p = proportion_row(base[stage].tolist(), base["symbol"].tolist())
                row.update(
                    conversion_from=prev,
                    conversion_rate=p["rate"],
                    conversion_wilson_lo=p["wilson_lo"],
                    conversion_wilson_hi=p["wilson_hi"],
                    conversion_cluster_lo=p.get("cluster_lo"),
                    conversion_cluster_hi=p.get("cluster_hi"),
                )
            elif len(c):
                row.update(
                    conversion_from="candidates_forecasts",
                    ratio_not_subset=len(members) / len(c),
                )
            if stage == "approved":
                row["detail"] = (
                    f"auto={int((members['auto_approved'].fillna('').str.startswith('yes')).sum())}; "
                    f"manual_approved_at={int(members['approved_at'].notna().sum())}"
                )
            if stage == "gate_pass":
                dropped = m[m["approved"] & ~m["gate_pass"]]
                row["detail"] = "approved_not_sent: " + "; ".join(
                    f"{k}={v}" for k, v in dropped["rung_state"].value_counts().items()
                )
                row["n_unmatched_ledger"] = int(m["unmatched_ledger"].sum())
            if stage == "accepted":
                unfilled = members[~members["filled"]]
                row["detail"] = "accepted_not_filled: " + "; ".join(
                    f"{k}={v}" for k, v in unfilled["rung_state"].value_counts().items()
                )
            rows.append(row)
            prev = stage
        approved = m[m["approved"]]
        budget = float(approved["budget"].sum())
        filled_amt = float(m["filled_amount"].sum())
        lo, hi = ratio_ci(
            approved["filled_amount"].tolist(), approved["budget"].tolist()
        )
        rows.append(
            {
                "market": market,
                "stage": "filled_amount_over_approved_budget",
                "n": int(m["filled"].sum()),
                "approved_budget": budget,
                "filled_amount": filled_amt,
                "ratio": filled_amt / budget if budget else np.nan,
                "ratio_lo": lo,
                "n_approved_budget_missing": int(approved["budget_missing"].sum()),
                "ratio_hi": hi,
                "ci_method": "percentile bootstrap over approved rungs",
                "currency": "USD" if market == "us" else "KRW",
            }
        )
        out[market] = pd.DataFrame(rows)
    return out
