"""Render the bakeoff scorecards as markdown tables."""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd


def _fmt_pct(v):
    return "" if pd.isna(v) else f"{v * 100:+.2f}%"


def _fmt_rate(v):
    return "" if pd.isna(v) else f"{v * 100:.0f}%"


def scorecard_table(
    dl: pd.DataFrame,
    bs: pd.DataFrame,
    market: str,
    horizon: int,
    gate: str,
    window: str = "all",
) -> str:
    q = dl[
        (dl.market == market)
        & (dl.horizon == horizon)
        & (dl.gate == gate)
        & (dl.window == window)
    ].copy()
    if q.empty:
        return "_(no rows)_\n"
    cols = ["source_id", "null_pctile"]
    extra = [
        c
        for c in (
            "block_null_pctile",
            "block_ci_crosses_zero",
            "block_source_ci_lo",
            "block_source_ci_hi",
        )
        if c in bs.columns
    ]
    b = bs[(bs.market == market) & (bs.horizon == horizon) & (bs.gate == gate)][
        cols + extra
    ]
    q = q.merge(b, on="source_id", how="left")
    q = q.sort_values("median_excess", ascending=False)
    lines = [
        "| 소스 | 결정일 | 중앙 수익 | 평균 수익 | **중앙 초과** | 평균 초과 | 시장이긴 날 | null 백분위 | block null | block CI∋0 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in q.itertuples():
        pct = "" if pd.isna(r.null_pctile) else f"{r.null_pctile * 100:.0f}"
        bp = getattr(r, "block_null_pctile", None)
        block_pct = "" if bp is None or pd.isna(bp) else f"{bp * 100:.0f}"
        crosses = getattr(r, "block_ci_crosses_zero", None)
        if crosses is None or (isinstance(crosses, float) and pd.isna(crosses)):
            cross_s = ""
        else:
            cross_s = "yes" if bool(crosses) else "no"
        lo = getattr(r, "block_source_ci_lo", None)
        hi = getattr(r, "block_source_ci_hi", None)
        if (
            lo is not None
            and hi is not None
            and not (isinstance(lo, float) and pd.isna(lo))
        ):
            cross_s = f"{cross_s} ({lo * 100:+.1f}…{hi * 100:+.1f})"
        lines.append(
            f"| `{r.source_id}` | {r.dates} | {_fmt_pct(r.median_ret)} | {_fmt_pct(r.mean_ret)} | "
            f"**{_fmt_pct(r.median_excess)}** | {_fmt_pct(r.mean_excess)} | "
            f"{_fmt_rate(r.dates_beating_market)} | {pct} | {block_pct} | {cross_s} |"
        )
    return "\n".join(lines) + "\n"


def gate_matrix(dl: pd.DataFrame, sc: pd.DataFrame, market: str, horizon: int) -> str:
    q = dl[(dl.market == market) & (dl.horizon == horizon) & (dl.window == "all")]
    piv = q.pivot_table(index="source_id", columns="gate", values="median_excess")
    pool = sc[(sc.market == market) & (sc.horizon == horizon) & (sc.window == "all")]
    gated = pool.pivot_table(index="source_id", columns="gate", values="mean_gated")
    gates = [
        g for g in ("none", "A_strong", "B_moderate2", "rsi45_only") if g in piv.columns
    ]
    head = (
        "| 소스 | "
        + " | ".join(f"{g} 중앙초과" for g in gates)
        + " | "
        + " | ".join(f"{g} 통과수/100" for g in gates if g != "none")
        + " |"
    )
    sep = "|---|" + "---:|" * (len(gates) + len([g for g in gates if g != "none"]))
    lines = [head, sep]
    for sid in piv.index:
        cells = [_fmt_pct(piv.loc[sid, g]) if g in piv.columns else "" for g in gates]
        gcells = [
            (
                f"{gated.loc[sid, g]:.1f}"
                if sid in gated.index
                and g in gated.columns
                and not pd.isna(gated.loc[sid, g])
                else ""
            )
            for g in gates
            if g != "none"
        ]
        lines.append(
            f"| `{sid}` | " + " | ".join(cells) + " | " + " | ".join(gcells) + " |"
        )
    return "\n".join(lines) + "\n"


def coverage_bias_section(scored: pd.DataFrame) -> str:
    """S2: missing/truncation is not random — report the directional split."""
    df = scored[
        (~scored["source_id"].str.endswith(".benchmark")) & (scored["gate"] == "none")
    ].copy()
    lines = [
        "## 누락/절단 방향성 편향 (S2)",
        "",
        "헤드라인은 미검열 행만 쓴다. 절단(`truncated` and not `censored`)과 "
        "봉 부재(`status=missing`)는 무작위 검열이 아니다.",
        "",
    ]
    for market, grp in df.groupby("market"):
        usable = grp[grp["horizon"] == 20]
        usable = usable[~usable["censored"].astype(bool)]
        usable = usable[usable["excess"].notna()]
        trunc = usable[
            (usable["status"] == "truncated") & (~usable["censored"].astype(bool))
        ]
        missing = grp[(grp["horizon"] == 20) & (grp["status"] == "missing")]

        def _summ(frame):
            if frame.empty or frame["excess"].isna().all():
                return "n=0"
            x = frame["excess"].to_numpy(dtype=float)
            x = x[~pd.isna(x)]
            return (
                f"n={len(x)}, mean excess {x.mean() * 100:+.3f}%, "
                f"median {np.median(x) * 100:+.3f}%, win {(x > 0).mean() * 100:.1f}%"
            )

        lines.append(f"### {market}")
        lines.append(f"- 전체 usable D+20: {_summ(usable)}")
        lines.append(f"- 실제 절단 (truncated, not censored) D+20: {_summ(trunc)}")
        if not trunc.empty and not usable.empty:
            t = trunc["excess"].mean()
            u = usable["excess"].mean()
            lines.append(
                f"- 절단 − 전체 mean excess: {(t - u) * 100:+.2f}pp "
                f"({'절단이 더 나쁨' if t < u else '절단이 더 좋음'})"
            )
        lines.append(f"- missing D+20 행: {len(missing)}")
        lines.append("")
    lines.extend(
        [
            "**경고 라벨:** `kr.oversold_recovery` (ETN/ELW 혼입, 픽의 31.8% 가 "
            "D+5 도 없어 부호 판정 불가), `kr.stable_growth` (30.0%, 동일). "
            "이 두 소스 수치는 나머지와 동급으로 읽으면 안 된다.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="research/screener_bakeoff/artifacts")
    args = ap.parse_args()
    d = pathlib.Path(args.dir)
    dl = pd.read_csv(d / "scorecard_datelevel.csv")
    sc = pd.read_csv(d / "scorecard.csv")
    bs = pd.read_csv(d / "bootstrap_null.csv")

    out = ["# screener bakeoff — 생성 표\n"]
    for market in ("kr", "us", "crypto"):
        gate0 = "none"
        for horizon in (5, 20):
            out.append(f"\n## {market.upper()} D+{horizon} — 게이트 없음 (전 기간)\n")
            out.append(scorecard_table(dl, bs, market, horizon, gate0))
            out.append(f"\n### {market.upper()} D+{horizon} — 최근 창 (recent)\n")
            out.append(scorecard_table(dl, bs, market, horizon, gate0, window="recent"))
        for horizon in (5, 20):
            out.append(f"\n## {market.upper()} D+{horizon} — 소스 × 게이트 매트릭스\n")
            out.append(gate_matrix(dl, sc, market, horizon))
    scored_path = d / "picks_scored.csv"
    if scored_path.exists():
        scored = pd.read_csv(scored_path)
        out.append("\n")
        out.append(coverage_bias_section(scored))
    (d / "report_tables.md").write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {d / 'report_tables.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
