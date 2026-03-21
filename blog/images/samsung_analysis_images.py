#!/usr/bin/env python3
"""삼성전자 종목 분석 블로그 이미지 생성기.

StockAnalysisPreset을 BlogImageGenerator 인터페이스로 래핑합니다.
컴포넌트 시스템을 사용하여 5장의 분석 이미지를 생성합니다.

사용법:
    python blog/images/samsung_analysis_images.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.image_generator import BlogImageGenerator  # noqa: E402
from blog.tools.presets.stock_analysis import StockAnalysisPreset  # noqa: E402


class SamsungAnalysisImages(BlogImageGenerator):
    """삼성전자 종합 분석 이미지 — StockAnalysisPreset 래퍼."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        images_dir: Path | None = None,
    ) -> None:
        super().__init__("samsung_analysis", images_dir)
        self.data = data or self._default_data()
        self._preset = StockAnalysisPreset(
            symbol="005930",
            data=self.data,
            output_dir=self.images_dir,
        )

    def get_images(self) -> list[tuple[str, int, int, Callable[[], str]]]:
        return [
            ("thumbnail", 1200, 630, self._preset._create_thumbnail),
            ("technical", 1400, 800, self._preset._create_technical),
            ("fundamental", 1400, 800, self._preset._create_fundamental),
            ("supply_demand", 1400, 800, self._preset._create_supply_demand),
            ("conclusion", 1400, 800, self._preset._create_conclusion),
        ]

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "company_profile": {
                "name": "삼성전자",
                "symbol": "005930",
                "sector": "반도체",
            },
            "indicators": {
                "rsi14": 57.16,
                "macd_histogram": -527,
                "macd_signal": "매도 신호",
                "adx": 16.37,
                "plus_di": 22.5,
                "minus_di": 18.3,
                "stoch_rsi_k": 0.78,
                "stoch_rsi_d": 0.65,
            },
            "valuation": {
                "per": 30.38,
                "pbr": 1.82,
                "roe": 6.01,
                "consensus_target": 85000,
                "current_price": 65800,
            },
            "financials": {
                "annual_earnings": [
                    {"year": "2021", "operating_income": 51_633_000_000_000},
                    {"year": "2022", "operating_income": 43_376_000_000_000},
                    {"year": "2023", "operating_income": 6_567_000_000_000},
                    {"year": "2024E", "operating_income": 32_700_000_000_000},
                ],
                "quarterly_margins": [
                    {"quarter": "Q1", "margin": 0.05},
                    {"quarter": "Q2", "margin": 0.08},
                    {"quarter": "Q3", "margin": 0.12},
                    {"quarter": "Q4E", "margin": 0.15},
                ],
            },
            "investor_trends": {
                "foreign_net": -15234,
                "institution_net": 8721,
                "individual_net": 6513,
                "foreign_consecutive_sell_days": 5,
            },
            "investment_opinions": {
                "opinions": [
                    {"firm": "삼성증권", "rating": "매수", "target": 90000},
                    {"firm": "NH투자", "rating": "매수", "target": 85000},
                    {"firm": "미래에셋", "rating": "중립", "target": 70000},
                ],
                "consensus": {"rating": "매수", "avg_target": 82000},
            },
            "support_resistance": {
                "supports": [62000, 58000, 55000],
                "resistances": [68000, 72000, 75000],
                "current_price": 65800,
            },
            "sector_peers": [
                {"name": "삼성전자", "market_cap": "350조", "per": "30.38"},
                {"name": "SK하이닉스", "market_cap": "140조", "per": "25.12"},
            ],
            "ohlcv": [
                {
                    "date": f"2024-01-{d:02d}",
                    "open": 65000 + d * 100,
                    "high": 66000 + d * 100,
                    "low": 64000 + d * 100,
                    "close": 65500 + d * 100,
                    "volume": 10000000 + d * 100000,
                }
                for d in range(2, 22)
            ],
        }


if __name__ == "__main__":
    SamsungAnalysisImages().generate()
