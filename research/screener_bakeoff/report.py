"""Render the bakeoff scorecards as markdown tables."""

from __future__ import annotations

import argparse
import pathlib

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
    b = bs[(bs.market == market) & (bs.horizon == horizon) & (bs.gate == gate)][
        ["source_id", "null_pctile"]
    ]
    q = q.merge(b, on="source_id", how="left")
    q = q.sort_values("median_excess", ascending=False)
    lines = [
        "| 소스 | 결정일 | 중앙 수익 | 평균 수익 | **중앙 초과** | 평균 초과 | 시장이긴 날 | null 백분위 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in q.itertuples():
        pct = "" if pd.isna(r.null_pctile) else f"{r.null_pctile * 100:.0f}"
        lines.append(
            f"| `{r.source_id}` | {r.dates} | {_fmt_pct(r.median_ret)} | {_fmt_pct(r.mean_ret)} | "
            f"**{_fmt_pct(r.median_excess)}** | {_fmt_pct(r.mean_excess)} | "
            f"{_fmt_rate(r.dates_beating_market)} | {pct} |"
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
    (d / "report_tables.md").write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {d / 'report_tables.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
