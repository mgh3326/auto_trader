"""Render the bootstrap annex tables from the *.bootstrap.json files."""

from __future__ import annotations

import argparse
import json
import os

MARKETS = ("kr", "us", "crypto_upbit_krw", "crypto_binance_usdt_spot")
TITLES = {
    "kr": "KR",
    "us": "US 🔴생존편향",
    "crypto_upbit_krw": "crypto/upbit",
    "crypto_binance_usdt_spot": "crypto/binance",
}
GAPS = (
    ("b_only_minus_a_and_b", "**B−A 대 A**"),
    ("b_only_minus_neither", "B−A 대 대조군"),
    ("a_and_b_minus_neither", "A 대 대조군"),
)


def _pp(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%p"


def _ci(bounds: list[float] | None) -> str:
    if not bounds:
        return "—"
    return f"[{bounds[0] * 100:+.2f}, {bounds[1] * 100:+.2f}]"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()
    for window, label in (("5", "D+5"), ("20", "D+20")):
        print(f"**{label} — 같은 날짜만 짝지은 중앙값 차이 (95% CI)**\n")
        print("| 시장 | 비교 | 공유 결정일 | n(좌) | n(우) | 점추정 | 95% CI | 좌측 우세 draw |")
        print("|---|---|---:|---:|---:|---:|---|---:|")
        for market in MARKETS:
            path = os.path.join(args.directory, f"{market}.bootstrap.json")
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                # A still-running annex writes an empty file; report it as
                # pending rather than crashing or silently omitting the row.
                print(f"| {TITLES[market]} | — | 부트스트랩 미완 | | | | | |")
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except json.JSONDecodeError:
                print(f"| {TITLES[market]} | — | 부트스트랩 미완 | | | | | |")
                continue
            block = data["windows"][window]
            for key, name in GAPS:
                row = block[key]
                print(
                    f"| {TITLES[market]} | {name} | {row['n_shared_dates']:,} | "
                    f"{row.get('n_left', 0):,} | {row.get('n_right', 0):,} | "
                    f"{_pp(row.get('median_diff_point'))} | "
                    f"{_ci(row.get('median_diff_ci95'))} | "
                    f"{row.get('share_of_draws_favouring_left', 0) * 100:.1f}% |"
                )
        print()


if __name__ == "__main__":
    main()
