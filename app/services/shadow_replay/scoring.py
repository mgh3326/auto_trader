"""ROB-697 (M1) — pure-function A' shadow replay decision scorer.

ROB-501 guard: stdlib + ``decimal.Decimal`` only. NO LLM, NO network, NO DB,
NO file I/O. Consumed by the corpus builder and the ``claude -p`` replay
driver (later tasks); this module has no dependency on either.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def extract_decision(item: Any) -> dict[str, Any]:
    side = _get(item, "side")
    max_action = _get(item, "max_action") or {}
    ev = _get(item, "evidence_snapshot") or {}
    setup = (ev.get("trade_setup") or {}) if isinstance(ev, dict) else {}
    headline = setup.get("headline") or {}
    triggers = _get(item, "trigger_checklist") or []
    return {
        "side": side if side in ("buy", "sell") else None,
        "notional": _dec(max_action.get("notional")),
        "quantity": _dec(max_action.get("quantity")),
        "limit_price": _dec(max_action.get("limit_price")),
        "entry": _dec(headline.get("entry")),
        "stop": _dec(setup.get("stop")),
        "target": _dec(setup.get("target")),
        "triggers": frozenset(str(t) for t in triggers),
        "proposer": (ev.get("proposer") if isinstance(ev, dict) else None),
    }


def _within(a: Decimal | None, b: Decimal | None, tol: Decimal) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def _size_band(a: dict, b: dict) -> bool:
    # same order of magnitude on notional (or both None); size is coarse by design
    an, bn = a.get("notional"), b.get("notional")
    if an is None or bn is None:
        return an is None and bn is None
    hi, lo = max(an, bn), min(an, bn)
    return lo > 0 and hi / lo <= Decimal("1.5")


def agree(
    a: dict, b: dict, *, tick: Decimal, atr: Decimal | None = None
) -> dict[str, Any]:
    side = a["side"] == b["side"]
    limit_tol = (atr / Decimal(4)) if atr else (tick * 3)
    limit = _within(a.get("limit_price"), b.get("limit_price"), limit_tol)
    ta, tb = a["triggers"], b["triggers"]
    jac = (len(ta & tb) / len(ta | tb)) if (ta or tb) else 1.0
    size_band = _size_band(a, b)
    same = side and size_band and limit and jac >= 0.6
    return {
        "side": side,
        "size_band": size_band,
        "limit": limit,
        "triggers_jaccard": jac,
        "same_decision": same,
    }


def summarize(
    decisions: list[dict],
    reference: dict | None,
    *,
    tick: Decimal,
    atr: Decimal | None = None,
) -> dict[str, Any]:
    k = len(decisions)
    no_action = sum(1 for d in decisions if d["side"] is None)
    # self-consistency: pairwise same_decision vs the modal decision (decisions[0] as anchor)
    anchor = decisions[0] if decisions else None
    self_same = (
        sum(
            1
            for d in decisions
            if anchor and agree(anchor, d, tick=tick, atr=atr)["same_decision"]
        )
        / k
        if k
        else 0.0
    )
    fidelity = None
    if reference is not None and k:
        matches = [agree(reference, d, tick=tick, atr=atr) for d in decisions]
        fidelity = {
            "side_rate": sum(m["side"] for m in matches) / k,
            "size_band_rate": sum(m["size_band"] for m in matches) / k,
            "limit_rate": sum(m["limit"] for m in matches) / k,
            "same_decision_rate": sum(m["same_decision"] for m in matches) / k,
        }
    return {
        "k": k,
        "no_action_rate": (no_action / k if k else 0.0),
        "self_same_decision_rate": self_same,
        "fidelity": fidelity,
    }
