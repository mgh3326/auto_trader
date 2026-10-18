"""evaluate (per-parent D+20 table) and report (M1/M2, Holm, stop, verdict).

Interim reports show counts and stop state only -- no metric, CI or p value --
because the prereg forbids stopping or tuning on interim significance. The
verdict exists only once the 20-session window is fully registered and every
assigned parent has reached D+20.

Verdict vocabulary (prereg §3; "인과 확증" is never used):
  진행 검토 후보   -- every candidate condition holds
  위험 신호로 종료 -- any stop latch, or M2_T < 0 (net loss)
  결론 유보       -- anything else (small sample, UNKNOWN > 0, Holm fail, ...)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow.costs import CostModel, FeeSchedule
from research.rob1391_us_resubmit_shadow.state import D, ParentState, State
from research.rob1391_us_resubmit_shadow.stats import (
    PairedRow,
    cluster_bootstrap,
    holm,
    paired_bootstrap,
)
from research.rob1391_us_resubmit_shadow.valuation import arm_net

CANDIDATE = "진행 검토 후보"
RISK = "위험 신호로 종료"
RESERVED = "결론 유보"
INTERIM = "INTERIM — 판정 없음(수집·관측 진행 중)"


def _model(state: State, source: str, slip: Decimal) -> CostModel:
    block = state.manifest.get("fee_schedule")
    schedule = FeeSchedule.parse(block["content"]) if block else None
    return CostModel(schedule=schedule, source=source, slippage_bp=slip)


def _matured(state: State, parent: ParentState) -> bool:
    return parent.rec["d20"] in state.stepped


def _aux_return(
    state: State, parent: ParentState, arm: str, model: CostModel
) -> str | None:
    """Fill-session + 20 auxiliary return on the arm's lots (separate metric)."""

    sessions = [s["day"] for s in state.manifest["calendar"]["sessions"]]
    gain = Decimal("0")
    basis = Decimal("0")
    for fill in parent.arms[arm].fills:
        i = sessions.index(fill["session"]) + K.AUX_FILL_OFFSET
        if i >= len(sessions):
            return None
        close = parent.marks.get(sessions[i])
        if close is None:
            return None
        qty, price = D(fill["qty"]), D(fill["price"])
        notional = qty * price
        exit_value = qty * D(close)
        gain += (
            exit_value
            - notional
            - model.buy_cost(notional)
            - model.sell_cost(exit_value, int(qty))
        )
        basis += notional
    return str(gain / basis) if basis else None


def evaluate(state: State) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for episode_id in state.assignment_order:
        parent = state.parents[episode_id]
        matured = _matured(state, parent)
        close = parent.marks.get(parent.rec["d20"]) if matured else None
        row: dict[str, Any] = {
            "episode_id": episode_id,
            "symbol": parent.rec["symbol"],
            "d0": parent.rec["d0"],
            "d20": parent.rec["d20"],
            "budget": parent.rec["budget"],
            "matured": matured,
            "d20_close": close,
            "real_followups": len(parent.real_followups),
            "review_block": parent.review_block,
            "unknown": {arm: parent.arms[arm].unknown for arm in ("T", "T_opt")},
        }
        for arm in ("C", "T", "T_opt"):
            row[f"fill_{arm}"] = str(parent.arms[arm].fill_notional)
            row[f"shares_{arm}"] = parent.arms[arm].shares
        for slip in (K.SLIPPAGE_BP_MAIN, *K.SLIPPAGE_BP_SENSITIVITY):
            model = _model(state, parent.rec["source"], slip)
            for arm in ("C", "T", "T_opt"):
                row[f"net_{arm}_slip{slip}"] = (
                    str(arm_net(parent.arms[arm], D(close), model))
                    if close is not None
                    else None
                )
        main = _model(state, parent.rec["source"], K.SLIPPAGE_BP_MAIN)
        for arm in ("C", "T"):
            row[f"aux_fill20_return_{arm}"] = _aux_return(state, parent, arm, main)
        rows.append(row)
    return rows


def _final(state: State) -> bool:
    window_done = all(s in state.registered for s in state.window)
    return window_done and all(_matured(state, p) for p in state.parents.values())


def _paired(
    rows: list[dict[str, Any]], kind: str, arm: str, *, slip: Decimal, cluster: str
) -> list[PairedRow]:
    out = []
    for r in rows:
        if kind == "m1":
            diff = float(D(r[f"fill_{arm}"]) - D(r["fill_C"]))
        else:
            diff = float(D(r[f"net_{arm}_slip{slip}"]) - D(r[f"net_C_slip{slip}"]))
        out.append(
            PairedRow(cluster=r[cluster], budget=float(D(r["budget"])), diff=diff)
        )
    return out


def _arm_ratio(rows: list[dict[str, Any]], key: str) -> float:
    denom = sum(float(D(r["budget"])) for r in rows)
    return sum(float(D(r[key])) for r in rows) / denom if denom else float("nan")


def build_report(state: State) -> dict[str, Any]:
    rows = evaluate(state)
    latches = [
        {"latch_type": x["latch_type"], "reason": x["reason"], "session": x["session"]}
        for x in state.latches
    ]
    counts = {
        "sessions_registered": len(state.registered),
        "window_sessions": len(state.window),
        "parents_assigned": len(rows),
        "parents_matured": sum(1 for r in rows if r["matured"]),
        "unknown_T": sum(1 for r in rows if r["unknown"]["T"]),
        "unknown_T_opt": sum(1 for r in rows if r["unknown"]["T_opt"]),
        "exclusions_by_reason": _exclusion_counts(state),
        "capital_reserved_max_fraction": _max_utilisation(state),
    }
    report: dict[str, Any] = {
        "experiment_id": state.manifest["experiment_id"],
        "epoch_id": state.manifest["epoch_id"],
        "net_metrics_status": state.manifest["net_metrics_status"],
        "deviations_approved": state.manifest["deviations"]["approved"],
        "latches": latches,
        "counts": counts,
    }
    if not _final(state):
        report["verdict"] = INTERIM
        return report

    main = K.SLIPPAGE_BP_MAIN
    fees_sealed = state.manifest["net_metrics_status"] == "SEALED"
    known = [r for r in rows if r["d20_close"] is not None and not r["unknown"]["T"]]
    unknown_ids = [r["episode_id"] for r in rows if r not in known]
    # Fixed denominator: every assigned parent, B_i once (prereg §3).
    metrics: dict[str, Any] = {
        "denominator_budget": sum(float(D(r["budget"])) for r in rows),
        "n_parents": len(rows),
        "n_symbols": len({r["symbol"] for r in rows}),
        "unknown_total": len(unknown_ids),
        "unknown_episodes": unknown_ids,
        "net_tests": "RUN" if fees_sealed else "WITHHELD_PENDING_FEE_SCHEDULE",
    }
    if unknown_ids:
        # UNKNOWN is never dropped from the primary set: the primary estimand is
        # undetermined, and the complete-case numbers are a labelled sensitivity.
        metrics["primary_status"] = "UNDETERMINED_UNKNOWN_PRESENT"
        metrics["primary"] = None
        metrics["holm"] = None
    else:
        metrics["primary_status"] = "DETERMINED"
        metrics.update(_arm_block(rows, fees_sealed, main))
        primary_in = {"delta_M1": _paired(rows, "m1", "T", slip=main, cluster="symbol")}
        if fees_sealed:
            primary_in["delta_M2"] = _paired(
                rows, "m2", "T", slip=main, cluster="symbol"
            )
        primary = paired_bootstrap(primary_in)
        metrics["primary"] = {k: _asdict(v) for k, v in primary.items()}
        metrics["holm"] = (
            holm({k: v.p_one_sided for k, v in primary.items()})
            if fees_sealed
            else None
        )
    metrics["sensitivity"] = _sensitivity(
        rows, known, bool(unknown_ids), fees_sealed, main
    )
    report["metrics"] = metrics
    report["verdict"], report["verdict_reasons"] = verdict(state, metrics)
    return report


def _arm_block(
    rows: list[dict[str, Any]], fees_sealed: bool, main: Decimal
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "M1_C": _arm_ratio(rows, "fill_C"),
        "M1_T": _arm_ratio(rows, "fill_T"),
    }
    if fees_sealed and rows:
        out["M2_C"] = _arm_ratio(rows, f"net_C_slip{main}")
        out["M2_T"] = _arm_ratio(rows, f"net_T_slip{main}")
    return out


def _sensitivity(
    rows: list[dict[str, Any]],
    known: list[dict[str, Any]],
    unknown_present: bool,
    fees_sealed: bool,
    main: Decimal,
) -> dict[str, Any]:
    """Not confirmatory. Every block runs on complete cases only (``known``)."""

    kinds = [("delta_M1", "m1")] + ([("delta_M2", "m2")] if fees_sealed else [])
    sens: dict[str, Any] = {}
    if unknown_present:
        sens["complete_case"] = {
            "n_parents": len(known),
            "denominator_budget": sum(float(D(r["budget"])) for r in known),
            **_arm_block(known, fees_sealed, main),
            **{
                k: _asdict(
                    cluster_bootstrap(
                        _paired(known, kind, "T", slip=main, cluster="symbol")
                    )
                )
                for k, kind in kinds
            },
        }
    sens["date_block"] = {
        k: _asdict(
            cluster_bootstrap(_paired(known, kind, "T", slip=main, cluster="d0"))
        )
        for k, kind in kinds
    }
    if fees_sealed:
        for slip in K.SLIPPAGE_BP_SENSITIVITY:
            sens[f"delta_M2_slip{slip}"] = _asdict(
                cluster_bootstrap(
                    _paired(known, "m2", "T", slip=slip, cluster="symbol")
                )
            )
    known_opt = [
        r for r in rows if r["d20_close"] is not None and not r["unknown"]["T_opt"]
    ]
    for k, kind in kinds:
        sens[f"optimistic_{k}"] = _asdict(
            cluster_bootstrap(
                _paired(known_opt, kind, "T_opt", slip=main, cluster="symbol")
            )
        )
    no_follow = [r for r in known if r["real_followups"] == 0]
    for k, kind in kinds:
        sens[f"exclude_real_followups_{k}"] = _asdict(
            cluster_bootstrap(
                _paired(no_follow, kind, "T", slip=main, cluster="symbol")
            )
        )
    return sens


def verdict(state: State, m: dict[str, Any]) -> tuple[str, list[str]]:
    if state.latches:
        return RISK, [f"stop:{x['latch_type']}:{x['reason']}" for x in state.latches]
    reasons: list[str] = []
    m2_t = m.get("M2_T")
    if m2_t is not None and m2_t == m2_t and m2_t < 0:
        return RISK, ["net_loss_M2_T_below_zero"]
    if m.get("net_tests") != "RUN":
        reasons.append("net_tests_withheld_pending_fee_schedule")
    if m.get("unknown_total", 0) != 0 or m.get("primary") is None:
        reasons.append("unknown_nonzero")
    if m["n_parents"] < K.MIN_PARENTS_FOR_CANDIDATE:
        reasons.append("parents_below_30")
    if m["n_symbols"] < K.MIN_SYMBOLS_FOR_CANDIDATE:
        reasons.append("symbols_below_10")
    holm_result = m.get("holm")
    if (
        not holm_result
        or set(holm_result) != {"delta_M1", "delta_M2"}
        or not all(holm_result.values())
    ):
        reasons.append("holm_not_passed")
    primary = m.get("primary") or {}
    if not (primary.get("delta_M1", {}).get("estimate", float("nan")) > 0):
        reasons.append("delta_M1_not_positive")
    if not (primary.get("delta_M2", {}).get("estimate", float("nan")) > 0):
        reasons.append("delta_M2_not_positive")
    if not (m2_t is not None and m2_t > 0):
        reasons.append("M2_T_not_positive")
    return (RESERVED, reasons) if reasons else (CANDIDATE, [])


def _asdict(result: Any) -> dict[str, Any]:
    return {name: getattr(result, name) for name in result.__slots__}


def _exclusion_counts(state: State) -> dict[str, int]:
    out: dict[str, int] = {}
    for payload in state.registered.values():
        for item in payload["exclusions"]:
            for reason in item.get("reasons", []):
                key = reason.split(":", 1)[0]
                out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _max_utilisation(state: State) -> float:
    best = 0.0
    for s in state.window:
        day = date.fromisoformat(s)
        reserved = sum(
            float(p.budget) for p in state.parents.values() if p.d0 <= day < p.d20
        )
        best = max(best, reserved / float(K.E0_USD))
    return best


def _fmt(value: Any) -> str:
    return f"{value:.5f}" if isinstance(value, float) else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['experiment_id']} — {report['epoch_id']}",
        "",
        f"- verdict: **{report['verdict']}**",
        f"- net metrics: {report['net_metrics_status']} · deviations: {report['deviations_approved']}",
        f"- latches: {report['latches'] or 'none'}",
        f"- counts: {report['counts']}",
    ]
    m = report.get("metrics")
    if m:
        lines += [
            "",
            "## Primary (conservative fill, 10bp/side)",
            f"- status {m['primary_status']} · net tests {m['net_tests']}",
            f"- parents {m['n_parents']} · symbols {m['n_symbols']} · fixed denominator ΣB "
            f"{m['denominator_budget']:.2f} · UNKNOWN {m['unknown_total']}",
        ]
        arms = {k: m[k] for k in ("M1_C", "M1_T", "M2_C", "M2_T") if k in m}
        if arms:
            lines.append("- " + " · ".join(f"{k} {_fmt(v)}" for k, v in arms.items()))
        for name, res in (m.get("primary") or {}).items():
            holm_pass = (m.get("holm") or {}).get(name, "withheld")
            lines.append(
                f"- {name}: {_fmt(res['estimate'])} (95% CI {_fmt(res['ci_low'])}–{_fmt(res['ci_high'])}), "
                f"one-sided p {_fmt(res['p_one_sided'])}, Holm pass {holm_pass}, clusters {res['n_clusters']}"
            )
        lines += ["", "## Sensitivity (not confirmatory; complete cases only)"]
        for name, res in m["sensitivity"].items():
            if "estimate" in res:
                lines.append(
                    f"- {name}: {_fmt(res['estimate'])} (CI {_fmt(res['ci_low'])}–{_fmt(res['ci_high'])})"
                )
            else:
                for sub, r in res.items():
                    if isinstance(r, dict) and "estimate" in r:
                        lines.append(
                            f"- {name}/{sub}: {_fmt(r['estimate'])} (CI {_fmt(r['ci_low'])}–{_fmt(r['ci_high'])})"
                        )
                    else:
                        lines.append(f"- {name}/{sub}: {_fmt(r)}")
        lines.append("")
        lines.append(f"- verdict reasons: {report.get('verdict_reasons')}")
        lines.append(
            "- 무작위화가 없는 짝지은 반사실 비교다. 인과 확증이 아니며, 통과해도 실거래 적용은 별도 승인 대상이다."
        )
    return "\n".join(lines) + "\n"
