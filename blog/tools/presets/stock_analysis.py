"""Stock analysis image set preset.

Generates 5 SVG images from MCP API response data:
1. Thumbnail (1200×630) — company name + key metrics
2. Technical (1400×800) — price chart + indicator dashboard + support/resistance
3. Fundamental (1400×800) — valuation cards + earnings chart + sector comparison
4. Supply & Demand (1400×800) — investor flow + opinion table + timeline
5. Conclusion (1400×800) — multi-perspective summary card
"""

from __future__ import annotations

from pathlib import Path

from blog.tools.components import SVGComponent, ThumbnailTemplate
from blog.tools.components.base import format_price
from blog.tools.stock import (
    ConclusionCard,
    EarningsChart,
    IndicatorDashboard,
    InvestorFlow,
    OpinionTable,
    PriceChart,
    SupportResistance,
    ValuationCards,
)


class StockAnalysisPreset:
    """MCP data → 5-image stock analysis set."""

    def __init__(
        self,
        symbol: str,
        data: dict,
        output_dir: Path | None = None,
    ) -> None:
        self.symbol = symbol
        self.data = data
        self.output_dir = output_dir or Path(__file__).parent.parent.parent / "images"
        self.output_dir.mkdir(exist_ok=True)

        self.company_name = data.get("company_profile", {}).get("name", symbol)

    def generate_svgs(self) -> list[Path]:
        """Generate 5 SVG files and return their paths."""
        images = [
            ("thumbnail", 1200, 630, self._create_thumbnail),
            ("technical", 1400, 800, self._create_technical),
            ("fundamental", 1400, 800, self._create_fundamental),
            ("supply_demand", 1400, 800, self._create_supply_demand),
            ("conclusion", 1400, 800, self._create_conclusion),
        ]

        paths: list[Path] = []
        for name, _w, _h, create_fn in images:
            svg_content = create_fn()
            file_path = self.output_dir / f"{self.symbol}_{name}.svg"
            file_path.write_text(svg_content, encoding="utf-8")
            paths.append(file_path)

        return paths

    async def generate_pngs(self) -> list[Path]:
        """Generate SVGs then convert to PNGs via SVGConverter."""
        from blog.tools.svg_converter import SVGConverter

        svg_paths = self.generate_svgs()
        converter = SVGConverter(self.output_dir)

        files = []
        for svg_path in svg_paths:
            png_name = svg_path.stem + ".png"
            width = 1200 if "thumbnail" in svg_path.stem else 1400
            files.append((svg_path.name, png_name, width))

        return await converter.convert_all(files)

    def _create_thumbnail(self) -> str:
        profile = self.data.get("company_profile", {})
        valuation = self.data.get("valuation", {})

        sector = profile.get("sector", "")
        current_price = valuation.get("current_price", 0)

        icons = [
            ("📊", "기술적 분석", "#2196F3"),
            ("💰", "펀더멘탈", "#4CAF50"),
            ("📈", "수급 분석", "#FF9800"),
            ("🎯", "종합 결론", "#9C27B0"),
        ]

        return ThumbnailTemplate.create(
            title_line1=f"{self.company_name} 종합 분석",
            title_line2=f"현재가 {format_price(current_price)}원",
            subtitle=f"{sector} | {self.symbol}",
            icons=icons,
            tech_stack="기술적 분석 • 펀더멘탈 • 수급 분석 • AI 종합 판단",
            accent_color="#4CAF50",
        )

    def _create_technical(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 기술적 분석")

        # Price chart (left, ~60% width)
        ohlcv = self.data.get("ohlcv", [])
        if ohlcv:
            svg += PriceChart.create(x=60, y=95, width=780, height=350, ohlcv=ohlcv)

        # Indicator dashboard (right, ~35% width)
        indicators = self.data.get("indicators", {})
        svg += IndicatorDashboard.create(x=880, y=95, width=480, height=350, indicators=indicators)

        # Support/resistance (bottom)
        sr = self.data.get("support_resistance", {})
        if sr:
            svg += SupportResistance.create(x=60, y=480, width=1300, height=280, **sr)

        svg += SVGComponent.footer()
        return svg

    def _create_fundamental(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 펀더멘탈 분석")

        # Valuation cards (top)
        valuation = self.data.get("valuation", {})
        svg += ValuationCards.create(x=60, y=95, width=1300, height=180, valuation=valuation)

        # Earnings chart (middle)
        financials = self.data.get("financials", {})
        if financials:
            svg += EarningsChart.create(x=60, y=310, width=1300, height=300, financials=financials)

        svg += SVGComponent.footer()
        return svg

    def _create_supply_demand(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 수급 분석")

        # Investor flow (top)
        investor = self.data.get("investor_trends", {})
        svg += InvestorFlow.create(x=60, y=95, width=1300, height=250, investor_trends=investor)

        # Opinion table (bottom)
        opinions = self.data.get("investment_opinions", {})
        svg += OpinionTable.create(x=60, y=380, width=1300, height=380, opinions=opinions)

        svg += SVGComponent.footer()
        return svg

    def _create_conclusion(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 종합 분석 결론")

        svg += ConclusionCard.create(
            x=60, y=95, width=1300, height=660,
            data=self.data,
            company_name=self.company_name,
        )

        svg += SVGComponent.footer()
        return svg
