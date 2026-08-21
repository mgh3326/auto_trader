"""Render the headline B-A vs A tables straight from the bootstrap outputs.

These two tables are the ones a reader acts on, so they must not be typed by
hand. An earlier revision carried a stale confidence interval into the primary
table because it was transcribed; this module removes that possibility.
"""

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


def _pp(value) -> str:
    return "—" if value is None else f"{float(value) * 100:+.2f}%p"


def _ci(bounds) -> str:
    if not bounds:
        return "—"
    return f"[{bounds[0] * 100:+.2f}, {bounds[1] * 100:+.2f}]"


def _verdict(bounds) -> str:
    if not bounds:
        return "—"
    if bounds[0] <= 0 <= bounds[1]:
        margin = min(abs(bounds[0]), abs(bounds[1])) * 100
        if margin < 0.02:
            return "판별 실패 (0 을 **간신히** 포함)"
        return "판별 실패"
    return "🔴 판별 성공 — 0 미포함"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()

    loaded = {}
    for market in MARKETS:
        path = os.path.join(args.directory, f"{market}.bootstrap.json")
        if os.path.exists(path) and os.path.getsize(path):
            with open(path, encoding="utf-8") as handle:
                loaded[market] = json.load(handle)

    print("**주 결과 — D+5 (공통 창, 검열 최소)**\n")
    print("| 시장 | 공유 결정일 | 점추정 | 95% CI | 판정 |")
    print("|---|---:|---:|---|---|")
    for market, payload in loaded.items():
        row = payload["windows"]["5"]["b_only_minus_a_and_b"]
        print(
            f"| {TITLES[market]} | {row['n_shared_dates']:,} | "
            f"{_pp(row.get('median_diff_point'))} | "
            f"{_ci(row.get('median_diff_ci95'))} | "
            f"{_verdict(row.get('median_diff_ci95'))} |"
        )
    print()

    print("**참고 — D+20 (🔴 §5.7 검열 보정 없이는 단독 인용 금지)**\n")
    print("| 시장 | 점추정 | i.i.d. 날짜 CI | 이동블록(4일) CI | 판정 |")
    print("|---|---:|---|---|---|")
    for market, payload in loaded.items():
        block = payload["windows"]["20"]
        row = block["b_only_minus_a_and_b"]
        moving = block.get("b_only_minus_a_and_b_moving_block", {})
        print(
            f"| {TITLES[market]} | {_pp(row.get('median_diff_point'))} | "
            f"{_ci(row.get('median_diff_ci95'))} | "
            f"{_ci(moving.get('median_diff_ci95'))} | "
            f"{_verdict(moving.get('median_diff_ci95') or row.get('median_diff_ci95'))} |"
        )
    print()


if __name__ == "__main__":
    main()
