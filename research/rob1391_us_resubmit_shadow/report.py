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
        "budget_rule": state.manifest["budget_rule"],
        "latches": latches,
        "counts": counts,
    }
    if not _final(state):
        report["verdict"] = INTERIM
        return report

    known = [r for r in rows if r["d20_close"] is not None and not r["unknown"]["T"]]
    unknown_close = [r["episode_id"] for r in rows if r["d20_close"] is None]
    main = K.SLIPPAGE_BP_MAIN
    metrics: dict[str, Any] = {
        "denominator_budget": sum(float(D(r["budget"])) for r in known),
        "n_parents": len(known),
        "n_symbols": len({r["symbol"] for r in known}),
        "unknown_total": counts["unknown_T"] + len(unknown_close),
        "M1_C": _arm_ratio(known, "fill_C"),
        "M1_T": _arm_ratio(known, "fill_T"),
        "M2_C": _arm_ratio(known, f"net_C_slip{main}") if known else float("nan"),
        "M2_T": _arm_ratio(known, f"net_T_slip{main}") if known else float("nan"),
    }
    primary = paired_bootstrap(
        {
            "delta_M1": _paired(known, "m1", "T", slip=main, cluster="symbol"),
            "delta_M2": _paired(known, "m2", "T", slip=main, cluster="symbol"),
        }
    )
    metrics["primary"] = {k: _asdict(v) for k, v in primary.items()}
    pvals = {k: v.p_one_sided for k, v in primary.items()}
    metrics["holm"] = holm(pvals)
    sens: dict[str, Any] = {}
    sens["date_block"] = {
        k: _asdict(
            cluster_bootstrap(_paired(known, kind, "T", slip=main, cluster="d0"))
        )
        for k, kind in (("delta_M1", "m1"), ("delta_M2", "m2"))
    }
    for slip in K.SLIPPAGE_BP_SENSITIVITY:
        sens[f"delta_M2_slip{slip}"] = _asdict(
            cluster_bootstrap(_paired(known, "m2", "T", slip=slip, cluster="symbol"))
        )
    known_opt = [
        r for r in rows if r["d20_close"] is not None and not r["unknown"]["T_opt"]
    ]
    sens["optimistic_delta_M1"] = _asdict(
        cluster_bootstrap(
            _paired(known_opt, "m1", "T_opt", slip=main, cluster="symbol")
        )
    )
    sens["optimistic_delta_M2"] = _asdict(
        cluster_bootstrap(
            _paired(known_opt, "m2", "T_opt", slip=main, cluster="symbol")
        )
    )
    no_follow = [r for r in known if r["real_followups"] == 0]
    sens["exclude_real_followups_delta_M1"] = _asdict(
        cluster_bootstrap(_paired(no_follow, "m1", "T", slip=main, cluster="symbol"))
    )
    sens["exclude_real_followups_delta_M2"] = _asdict(
        cluster_bootstrap(_paired(no_follow, "m2", "T", slip=main, cluster="symbol"))
    )
    metrics["sensitivity"] = sens
    report["metrics"] = metrics
    report["verdict"], report["verdict_reasons"] = verdict(state, metrics)
    return report


def verdict(state: State, m: dict[str, Any]) -> tuple[str, list[str]]:
    if state.latches:
        return RISK, [f"stop:{x['latch_type']}:{x['reason']}" for x in state.latches]
    reasons: list[str] = []
    fees_sealed = state.manifest["net_metrics_status"] == "SEALED"
    if fees_sealed and m["M2_T"] == m["M2_T"] and m["M2_T"] < 0:
        return RISK, ["net_loss_M2_T_below_zero"]
    if not fees_sealed:
        reasons.append("net_tests_withheld_pending_fee_schedule")
    if m["n_parents"] < K.MIN_PARENTS_FOR_CANDIDATE:
        reasons.append("parents_below_30")
    if m["n_symbols"] < K.MIN_SYMBOLS_FOR_CANDIDATE:
        reasons.append("symbols_below_10")
    if not all(m["holm"].values()):
        reasons.append("holm_not_passed")
    if not m["primary"]["delta_M1"]["estimate"] > 0:
        reasons.append("delta_M1_not_positive")
    if not m["primary"]["delta_M2"]["estimate"] > 0:
        reasons.append("delta_M2_not_positive")
    if not m["M2_T"] > 0:
        reasons.append("M2_T_not_positive")
    if m["unknown_total"] != 0:
        reasons.append("unknown_nonzero")
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


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['experiment_id']} — {report['epoch_id']}",
        "",
        f"- verdict: **{report['verdict']}**",
        f"- net metrics: {report['net_metrics_status']} · budget rule: {report['budget_rule']}",
        f"- latches: {report['latches'] or 'none'}",
        f"- counts: {report['counts']}",
    ]
    if "metrics" in report:
        m = report["metrics"]
        lines += [
            "",
            "## Primary (conservative fill, 10bp/side)",
            f"- parents {m['n_parents']} · symbols {m['n_symbols']} · denominator ΣB {m['denominator_budget']:.2f} · UNKNOWN {m['unknown_total']}",
            f"- M1_C {m['M1_C']:.4f} · M1_T {m['M1_T']:.4f} · M2_C {m['M2_C']:.4f} · M2_T {m['M2_T']:.4f}",
        ]
        for name, res in m["primary"].items():
            lines.append(
                f"- {name}: {res['estimate']:.5f} (95% CI {res['ci_low']:.5f}–{res['ci_high']:.5f}), "
                f"one-sided p {res['p_one_sided']:.4f}, Holm pass {m['holm'][name]}, clusters {res['n_clusters']}"
            )
        lines += ["", "## Sensitivity (not confirmatory)"]
        for name, res in m["sensitivity"].items():
            if isinstance(res, dict) and "estimate" in res:
                lines.append(
                    f"- {name}: {res['estimate']:.5f} (CI {res['ci_low']:.5f}–{res['ci_high']:.5f})"
                )
            else:
                for sub, r in res.items():
                    lines.append(
                        f"- {name}/{sub}: {r['estimate']:.5f} (CI {r['ci_low']:.5f}–{r['ci_high']:.5f})"
                    )
        lines.append("")
        lines.append(f"- verdict reasons: {report.get('verdict_reasons')}")
        lines.append(
            "- 무작위화가 없는 짝지은 반사실 비교다. 인과 확증이 아니며, 통과해도 실거래 적용은 별도 승인 대상이다."
        )
    return "\n".join(lines) + "\n"
