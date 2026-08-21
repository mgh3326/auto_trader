"""Render the B2 censoring tables from the *.censoring.json files."""

from __future__ import annotations

import argparse
import json
import os

MARKETS = ("kr", "us", "crypto_upbit_krw", "crypto_binance_usdt_spot")
TITLES = {
    "kr": "KR",
    "us": "US 🔴생존편향",
    "crypto_upbit_krw": "crypto/upbit 🔴비정본",
    "crypto_binance_usdt_spot": "crypto/binance 🔴비정본",
}
LABELS = {
    "a_and_b": "A",
    "b_only": "B−A",
    "neither": "대조군",
}


def _pct(value) -> str:
    return "—" if value is None else f"{float(value) * 100:+.2f}%"


def _pp(value) -> str:
    """A difference between two returns is percentage POINTS, not percent."""
    return "—" if value is None else f"{float(value) * 100:+.3f}%p"


def _rate(value) -> str:
    return "—" if value is None else f"{float(value) * 100:.4f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()

    print("**(1) D+20 채점불가 표본의 분해 — 끝단 절단 대 정보성 검열**\n")
    print("| 시장 | 코호트 | 전체 | corpus_end | terminal_gap | terminal_gap 비율 |")
    print("|---|---|---:|---:|---:|---:|")
    payloads = {}
    for market in MARKETS:
        path = os.path.join(args.directory, f"{market}.censoring.json")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            print(f"| {TITLES[market]} | — | 미산출 | | | |")
            continue
        with open(path, encoding="utf-8") as handle:
            payloads[market] = json.load(handle)
        for cohort in ("a_and_b", "b_only", "neither"):
            block = payloads[market]["cohorts"][cohort]
            buckets = block["buckets"]
            print(
                f"| {TITLES[market]} | {LABELS[cohort]} | {block['n_total']:,} | "
                f"{buckets.get('corpus_end', 0):,} | "
                f"{buckets.get('terminal_gap', 0):,} | "
                f"{_rate(block['terminal_gap_rate'])} |"
            )
    print()

    print("**(2) terminal_gap 표본이 남긴 D+5 흔적 (사라진 D+20 의 방향 단서)**\n")
    print("| 시장 | 코호트 | n(D+5 관측) | D+5 중앙값 | D+5 평균 | 승률 |")
    print("|---|---|---:|---:|---:|---:|")
    for market, payload in payloads.items():
        for cohort in ("a_and_b", "b_only"):
            stat = payload["cohorts"][cohort]["terminal_gap_observable_d5"]
            if not stat.get("n"):
                print(f"| {TITLES[market]} | {LABELS[cohort]} | 0 | — | — | — |")
                continue
            print(
                f"| {TITLES[market]} | {LABELS[cohort]} | {stat['n']:,} | "
                f"{_pct(stat['median'])} | {_pct(stat['mean'])} | "
                f"{float(stat['win_rate']) * 100:.1f}% |"
            )
    print()

    print("**(3) D+20 유계 민감도 — 검열 표본에 가정을 넣었을 때의 중앙값 범위**\n")
    print("🔴 각 열은 **가정**이지 관측이 아니다. 보고된 D+20 은 검열분을 뺀 값이다.\n")
    print("| 시장 | 코호트 | 보고값(검열 제외) | 최악(전손) | D+5 승계 | 최선(관측 최대) |")
    print("|---|---|---:|---:|---:|---:|")
    for market, payload in payloads.items():
        for cohort in ("a_and_b", "b_only"):
            block = payload["cohorts"][cohort]
            sens = block["d20_bounded_sensitivity"]
            print(
                f"| {TITLES[market]} | {LABELS[cohort]} | "
                f"{_pct(block['d20_as_reported_excludes_censored'].get('median'))} | "
                f"{_pct(sens['assumption_worst_total_loss'].get('median'))} | "
                f"{_pct(sens['assumption_carry_d5_else_observed_min'].get('median'))} | "
                f"{_pct(sens['assumption_best_observed_max'].get('median'))} |"
            )
    print()

    print("**(4) 🔴 핵심 — 보고 대상인 *격차* 가 검열 가정에 얼마나 흔들리나**\n")
    print("각 팔을 따로 묶는 것만으로는 \"검열이 결론을 뒤집을 수 있나\"에 답이 안 된다.")
    print("아래는 **B−A 마이너스 A** 자체를 가정별로 다시 계산한 것이다.\n")
    print("| 시장 | 가정 | 중앙값 격차 | 평균 격차 |")
    print("|---|---|---:|---:|")
    names = {
        "reported_excludes_censored": "보고값(검열 제외)",
        "assumption_worst_total_loss": "최악 — 전손",
        "assumption_carry_d5_else_observed_min": "D+5 승계",
        "assumption_best_observed_max": "최선 — 관측 최대",
    }
    for market, payload in payloads.items():
        bounds = payload.get("b_only_minus_a_gap_under_bounds", {})
        for key, label in names.items():
            row = bounds.get(key, {})
            print(
                f"| {TITLES[market]} | {label} | {_pp(row.get('median'))} | "
                f"{_pp(row.get('mean'))} |"
            )
    print()

    print("**(5) 검열 비대칭 방향**\n")
    print("| 시장 | A terminal_gap | B−A terminal_gap | B−A / A | 방향 |")
    print("|---|---:|---:|---:|---|")
    for market, payload in payloads.items():
        asym = payload["asymmetry"]
        ratio = asym.get("b_only_over_a_ratio")
        print(
            f"| {TITLES[market]} | {_rate(asym['terminal_gap_rate_a_and_b'])} | "
            f"{_rate(asym['terminal_gap_rate_b_only'])} | "
            f"{'—' if ratio is None else f'{ratio:.2f}x'} | "
            f"{'B−A 가 더 잃음' if asym['direction'].startswith('b_only') else 'A 가 더 잃음' if asym['direction'].startswith('a_and_b') else '동일'} |"
        )
    print()


if __name__ == "__main__":
    main()
