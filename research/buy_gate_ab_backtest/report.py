"""Render the markdown result tables from run_backtest JSON. No re-computation.

    uv run python -m research.buy_gate_ab_backtest.report DIR > report.md

Every number printed here is copied from a ``run_backtest`` result file, which
in turn copied it from the pre-registered ``score_window``. This module does
arithmetic only for percentage formatting.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

COHORT_ROWS = (
    ("a_and_b", "A 통과 (라이브 게이트)"),
    ("b_only", "**B−A** (strong 부재로 A가 기각)"),
    ("neither", "대조군 (양쪽 기각)"),
)
MARKET_TITLES = {
    "kr": "KR (kr-corpus-v1, KOSPI+KOSDAQ)",
    "us": "US (us-corpus-v1) — 🔴 생존편향",
    "crypto_upbit_krw": (
        "부록(🔴비정본): crypto / upbit_krw — upstream spec 시장 밖, "
        "정책 근거 사용 금지"
    ),
    "crypto_binance_usdt_spot": (
        "부록(🔴비정본): crypto / binance_usdt_spot — upstream spec 시장 밖, "
        "센트 반올림 왜곡 큼(§5.8), 정책 근거 사용 금지"
    ),
}


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:+.2f}%"


def _rate(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _window_table(summary: dict[str, Any], window: str) -> list[str]:
    lines = [
        f"| 코호트 | n(제출) | n(채점가능) | 중앙값 | 평균 | 승률 | MDD 중앙값 | p10 | p90 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in COHORT_ROWS:
        block = summary["cohorts"][key]["windows"][window]
        ret = block["simple_return_to_close"]
        mdd = block["max_drawdown_from_entry_close_peak"]
        lines.append(
            f"| {label} | {block['n_submitted']:,} | {block['n_scoreable']:,} | "
            f"{_pct(ret.get('median'))} | {_pct(ret.get('mean'))} | "
            f"{_rate(block.get('win_rate_return_gt_zero'))} | "
            f"{_pct(mdd.get('median'))} | {_pct(ret.get('p10'))} | "
            f"{_pct(ret.get('p90'))} |"
        )
    return lines


def render_market(result: dict[str, Any]) -> str:
    summary = result["summary"]
    market = result["market"]
    lines: list[str] = [f"### {MARKET_TITLES.get(market, market)}", ""]
    lines += [
        "| 항목 | 값 |",
        "|---|---|",
        f"| corpus | `{result['corpus_id']}` (main scope, holdout 미개봉) |",
        f"| 창 | {result['corpus_first_session']} … {result['corpus_last_session']} |",
        f"| 채점 as-of (단일) | {result['scoring_as_of'][:10]} |",
        f"| 결정일 수 (eligible) | {result['decision_sessions']:,} |",
        f"| phase 그리드 총수 (참고, S3) | {result.get('phase_sessions_total', '—'):,} |",
        f"| corpus 행/종목 | {result['corpus_rows']:,} / {result['corpus_symbols']:,} |",
        f"| 결정일 유니버스 행 | {result['universe_rows_at_decision_dates']:,} |",
        f"| 유동성 하한 미달로 제외 | {result['rejected_below_liquidity_floor']:,} |",
        f"| 평가된 표본 | {sum(summary['cohorts'][key]['n'] for key, _ in COHORT_ROWS):,} |",
        f"| 재구성 실패 | {result['reconstruction_failures'] or '없음'} |",
        f"| addendum digest | `{result['addendum_sha256'][:8]}…` (최초 freeze "
        f"`{result.get('first_freeze_addendum_sha256', '')[:8]}…`) |",
        "",
    ]
    for window, label in (("5", "D+5"), ("20", "D+20")):
        lines.append(f"**{label}**")
        lines.append("")
        lines += _window_table(summary, window)
        lines.append("")
    lines.append("대조군 기각 사유 분포(중복 계수): "
                 f"`{summary['control_reject_reason_histogram']}`")
    lines.append("")
    strengths = {
        key: summary["cohorts"][key]["support_strength_histogram"]
        for key, _ in COHORT_ROWS
    }
    lines.append(f"코호트별 지지 강도 분포: `{strengths}`")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()
    for market in ("kr", "us", "crypto_upbit_krw", "crypto_binance_usdt_spot"):
        path = os.path.join(args.directory, f"{market}.json")
        if not os.path.exists(path):
            print(f"### {MARKET_TITLES[market]}\n\n미산출 (결과 파일 없음)\n")
            continue
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
        print(render_market(result))


if __name__ == "__main__":
    main()
