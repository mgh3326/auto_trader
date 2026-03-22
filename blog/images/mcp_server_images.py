#!/usr/bin/env python3
"""
MCP 서버 블로그 이미지 생성기

사용법:
    uv run python blog/images/mcp_server_images.py
"""

import sys
from pathlib import Path
from typing import override

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.components.base import FONT_FAMILY, SVGComponent
from blog.tools.components.thumbnail import ThumbnailTemplate
from blog.tools.image_generator import BlogImageGenerator


class MCPServerImages(BlogImageGenerator):
    """MCP 서버 블로그 이미지 생성기"""

    @override
    def get_images(self):
        return [
            ("thumbnail", 1200, 630, self.create_thumbnail),
            ("architecture", 1400, 900, self.create_architecture),
            ("routing", 1200, 700, self.create_routing),
        ]

    def create_thumbnail(self) -> str:
        """썸네일 이미지 (1200x630)"""
        return ThumbnailTemplate.create(
            title_line1="MCP 서버로 AI 트레이딩",
            title_line2="도구 만들기",
            subtitle="Claude가 직접 주식을 분석하고 매매하는 시스템",
            icons=[
                ("server", "MCP", "#2196F3"),
                ("code", "API", "#4CAF50"),
                ("database", "DB", "#FF9800"),
            ],
            theme="terminal",
            bg_pattern="grid",
            accent_color="#00d4aa",
        )

    def create_architecture(self) -> str:
        """아키텍처 다이어그램 (1400x900)"""
        from blog.tools.components.flow_diagram import FlowDiagram

        nodes = [
            (500, 80, 400, 70, "Claude Desktop / Claude Code", "#5436DA"),
            (520, 205, 360, 60, "FastMCP Server (Port 8765)", "#2196F3"),
            (520, 290, 360, 55, "Symbol Router", "#546E7A"),
            (120, 380, 230, 62, "Market Data Tools", "#1976D2"),
            (380, 380, 230, 62, "Portfolio Tools", "#388E3C"),
            (640, 380, 230, 62, "Trading Tools", "#F57C00"),
            (900, 380, 230, 62, "Analytics + AI", "#8E24AA"),
            (160, 520, 300, 70, "KIS API", "#FF6B00"),
            (550, 520, 300, 70, "Yahoo + Naver + Finnhub", "#6001D2"),
            (940, 520, 300, 70, "Upbit + Binance + CoinGecko", "#093687"),
            (260, 690, 360, 80, "PostgreSQL", "#607D8B"),
            (760, 690, 360, 80, "Redis", "#D32F2F"),
        ]
        edges = [
            (0, 1, "MCP Protocol"),
            (1, 2, "35 Tools"),
            (2, 3, "quote / ohlcv"),
            (2, 4, "holdings"),
            (2, 5, "orders"),
            (2, 6, "analysis"),
            (3, 7, ""),
            (3, 8, ""),
            (3, 9, ""),
            (4, 10, ""),
            (5, 10, ""),
            (5, 11, "limits"),
            (6, 8, ""),
            (6, 9, ""),
        ]

        svg = SVGComponent.header(1400, 900)
        svg += SVGComponent.background(1400, 900)
        svg += SVGComponent.title(
            1400, "MCP 서버 아키텍처 — 35개 도구, 7개 데이터 소스", y=45, font_size=30
        )
        svg += """
    <rect x="90" y="190" width="1220" height="280" fill="#ffffff" stroke="#2196F3" stroke-width="2" rx="10"/>
    <rect x="90" y="190" width="1220" height="40" fill="#2196F3" rx="10"/>
    <text x="700" y="216" {FONT_FAMILY} font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">auto_trader-mcp (FastMCP Server)</text>

    <rect x="120" y="500" width="1160" height="110" fill="#ffffff" stroke="#D0D7DE" stroke-width="1" rx="8"/>
    <text x="700" y="492" {FONT_FAMILY} font-size="14" font-weight="bold" fill="#666666" text-anchor="middle">External Data Sources</text>

    <rect x="220" y="675" width="980" height="115" fill="#ffffff" stroke="#D0D7DE" stroke-width="1" rx="8"/>
    <text x="710" y="668" {FONT_FAMILY} font-size="13" font-weight="bold" fill="#666666" text-anchor="middle">State Stores</text>
"""
        svg += FlowDiagram.create(nodes=nodes, edges=edges)
        svg += """
    <text x="700" y="118" {FONT_FAMILY} font-size="13" fill="#c4b8ff" text-anchor="middle">MCP 클라이언트</text>
    <text x="700" y="325" {FONT_FAMILY} font-size="11" fill="#CFD8DC" text-anchor="middle">_resolve_market_type() · market aliases · validation</text>

    <text x="235" y="545" {FONT_FAMILY} font-size="11" fill="#FFE0B2" text-anchor="middle">국내/해외 주식 시세 · 보유 · 주문</text>
    <text x="700" y="545" {FONT_FAMILY} font-size="11" fill="#D1B3FF" text-anchor="middle">해외/국내 펀더멘털 · 뉴스 · 실적 · 밸류에이션</text>
    <text x="1090" y="545" {FONT_FAMILY} font-size="11" fill="#B3C7E6" text-anchor="middle">암호화폐 시세/주문 + 김치프리미엄 분석</text>

    <text x="440" y="735" {FONT_FAMILY} font-size="12" fill="#CFD8DC" text-anchor="middle">manual_holdings · broker_accounts · stock_aliases</text>
    <text x="940" y="735" {FONT_FAMILY} font-size="12" fill="#FFCDD2" text-anchor="middle">daily_order_count · model_rate_limit</text>

    <text x="700" y="838" {FONT_FAMILY} font-size="14" fill="#666666" text-anchor="middle">Claude → MCP Protocol → FastMCP Server → external APIs + DB/Redis</text>
    <text x="700" y="862" {FONT_FAMILY} font-size="12" fill="#999999" text-anchor="middle">심볼 포맷 자동 라우팅 · dry_run 기본 안전 모드 · 비동기 병렬 처리</text>
"""
        svg += SVGComponent.footer()
        return svg

    def create_routing(self) -> str:
        """심볼 라우팅 다이어그램 (1200x700)"""
        from blog.tools.components.flow_diagram import FlowDiagram

        nodes = [
            (430, 90, 340, 55, "get_quote(symbol, market?)", "#5436DA"),
            (350, 195, 500, 60, "_resolve_market_type(symbol, market)", "#546E7A"),
            (80, 320, 260, 90, "equity_kr", "#1976D2"),
            (470, 320, 260, 90, "equity_us", "#388E3C"),
            (860, 320, 260, 90, "crypto", "#F57C00"),
            (80, 500, 260, 90, "KIS API + Naver", "#FF6B00"),
            (460, 500, 280, 90, "Yahoo Finance + Finnhub", "#6001D2"),
            (860, 500, 280, 90, "Upbit + Binance/CoinGecko", "#093687"),
        ]
        edges = [
            (0, 1, "MCP tool call"),
            (1, 2, "6-digit/alnum"),
            (1, 3, "ticker/symbol"),
            (1, 4, "KRW-/USDT-"),
            (2, 5, ""),
            (3, 6, ""),
            (4, 7, ""),
        ]

        svg = SVGComponent.header(1200, 700)
        svg += SVGComponent.background(1200, 700, fill="#ffffff")
        svg += SVGComponent.title(1200, "지능형 심볼 라우팅 시스템", y=40, font_size=26)
        svg += """
    <text x="600" y="65" {FONT_FAMILY} font-size="14" fill="#888888" text-anchor="middle">심볼 포맷에 따라 자동으로 시장과 데이터 소스를 결정</text>
    <rect x="100" y="620" width="1000" height="55" fill="#F5F5F5" stroke="#E0E0E0" stroke-width="1" rx="6"/>
"""
        svg += FlowDiagram.create(nodes=nodes, edges=edges)
        svg += """
    <text x="600" y="136" {FONT_FAMILY} font-size="12" fill="#c4b8ff" text-anchor="middle">Claude가 호출하는 MCP 도구</text>
    <text x="600" y="240" {FONT_FAMILY} font-size="12" fill="#CFD8DC" text-anchor="middle">market 명시 → 검증 | market 생략 → 자동 감지</text>

    <text x="210" y="372" font-family="monospace" font-size="12" fill="#BBDEFB" text-anchor="middle">005930 / 0123G0</text>
    <text x="600" y="372" font-family="monospace" font-size="12" fill="#C8E6C9" text-anchor="middle">AAPL / BRK.B</text>
    <text x="990" y="372" font-family="monospace" font-size="12" fill="#FFE0B2" text-anchor="middle">KRW-BTC / USDT-ETH</text>

    <text x="210" y="555" {FONT_FAMILY} font-size="11" fill="#FFE0B2" text-anchor="middle">한국투자증권 + 펀더멘털</text>
    <text x="600" y="555" {FONT_FAMILY} font-size="11" fill="#D1B3FF" text-anchor="middle">yfinance + 뉴스/실적/내부자거래</text>
    <text x="1000" y="555" {FONT_FAMILY} font-size="11" fill="#B3C7E6" text-anchor="middle">업비트 + 글로벌 온체인/거래소 데이터</text>

    <text x="120" y="645" {FONT_FAMILY} font-size="12" font-weight="bold" fill="#333333">market aliases:</text>
    <text x="230" y="645" font-family="monospace" font-size="11" fill="#1565C0">kr, krx, kospi, kosdaq → equity_kr</text>
    <text x="555" y="645" font-family="monospace" font-size="11" fill="#2E7D32">us, nasdaq, nyse, yahoo → equity_us</text>
    <text x="870" y="645" font-family="monospace" font-size="11" fill="#EF6C00">crypto, upbit, krw → crypto</text>
    <text x="600" y="665" {FONT_FAMILY} font-size="11" fill="#999999" text-anchor="middle">market 파라미터를 명시하면 심볼 형식을 해당 시장 규칙으로 검증</text>
"""
        svg += SVGComponent.footer()
        return svg


if __name__ == "__main__":
    MCPServerImages("mcp_server").generate()
