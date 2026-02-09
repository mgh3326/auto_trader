#!/usr/bin/env python3
"""
MCP 서버 블로그 이미지 생성기

사용법:
    uv run python blog/images/mcp_server_images.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate


class MCPServerImages(BlogImageGenerator):
    """MCP 서버 블로그 이미지 생성기"""

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
                ("🤖", "Claude", "#5436DA"),
                ("📊", "35 Tools", "#2196F3"),
                ("📈", "Trading", "#4CAF50"),
                ("🔗", "7 APIs", "#FF9800"),
            ],
            tech_stack="FastMCP • KIS • Upbit • Yahoo • Naver • Finnhub • CoinGecko",
            bg_gradient=("#0a0e27", "#1a1f4e", "#2d3a8c"),
            accent_color="#00d4aa",
        )

    def create_architecture(self) -> str:
        """아키텍처 다이어그램 (1400x900)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1400" height="900" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#666666" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1400" height="900" fill="#f8f9fa"/>

    <!-- 제목 -->
    <text x="700" y="45" font-family="Arial, sans-serif" font-size="30" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        MCP 서버 아키텍처 — 35개 도구, 7개 데이터 소스
    </text>

    <!-- Claude / AI 클라이언트 -->
    <rect x="530" y="70" width="340" height="70" fill="#5436DA" stroke="#3d2a9e" stroke-width="2" rx="10"/>
    <text x="700" y="100" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">Claude Desktop / Claude Code</text>
    <text x="700" y="125" font-family="Arial, sans-serif" font-size="13" fill="#c4b8ff" text-anchor="middle">MCP 클라이언트</text>

    <!-- 화살표: Claude -> MCP 서버 -->
    <line x1="700" y1="140" x2="700" y2="175" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <text x="750" y="163" font-family="Arial, sans-serif" font-size="11" fill="#888888">MCP Protocol</text>

    <!-- MCP 서버 큰 박스 -->
    <rect x="100" y="180" width="1200" height="280" fill="#ffffff" stroke="#2196F3" stroke-width="2" rx="10"/>
    <rect x="100" y="180" width="1200" height="40" fill="#2196F3" rx="10"/>
    <rect x="100" y="210" width="1200" height="10" fill="#2196F3"/>
    <text x="700" y="207" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">auto_trader-mcp (FastMCP Server) — Port 8765</text>

    <!-- 도구 카테고리 박스들 (1행) -->
    <rect x="130" y="235" width="160" height="90" fill="#E3F2FD" stroke="#1976D2" stroke-width="1" rx="6"/>
    <text x="210" y="260" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#1565C0" text-anchor="middle">시장 데이터</text>
    <text x="210" y="278" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">search_symbol</text>
    <text x="210" y="293" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_quote</text>
    <text x="210" y="308" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_ohlcv</text>

    <rect x="310" y="235" width="160" height="90" fill="#E8F5E9" stroke="#388E3C" stroke-width="1" rx="6"/>
    <text x="390" y="260" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#2E7D32" text-anchor="middle">포트폴리오</text>
    <text x="390" y="278" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_holdings</text>
    <text x="390" y="293" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_position</text>
    <text x="390" y="308" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">update_manual +2</text>

    <rect x="490" y="235" width="160" height="90" fill="#FFF3E0" stroke="#F57C00" stroke-width="1" rx="6"/>
    <text x="570" y="260" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#EF6C00" text-anchor="middle">매매 실행</text>
    <text x="570" y="278" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">place_order</text>
    <text x="570" y="293" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">create_dca_plan</text>
    <text x="570" y="308" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">cancel_order +1</text>

    <rect x="670" y="235" width="160" height="90" fill="#FCE4EC" stroke="#C2185B" stroke-width="1" rx="6"/>
    <text x="750" y="260" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#AD1457" text-anchor="middle">기술적 분석</text>
    <text x="750" y="278" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_indicators</text>
    <text x="750" y="293" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_volume_profile</text>
    <text x="750" y="308" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_fibonacci +1</text>

    <!-- 도구 카테고리 박스들 (2행) -->
    <rect x="130" y="340" width="340" height="90" fill="#F3E5F5" stroke="#7B1FA2" stroke-width="1" rx="6"/>
    <text x="300" y="365" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#6A1B9A" text-anchor="middle">펀더멘털 분석 (12개)</text>
    <text x="210" y="385" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_company_profile</text>
    <text x="210" y="400" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_financials</text>
    <text x="210" y="415" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_valuation</text>
    <text x="390" y="385" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_investor_trends</text>
    <text x="390" y="400" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_sector_peers</text>
    <text x="390" y="415" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_news +6 more</text>

    <rect x="490" y="340" width="340" height="90" fill="#E0F7FA" stroke="#00838F" stroke-width="1" rx="6"/>
    <text x="660" y="365" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#006064" text-anchor="middle">시장 분석 (6개)</text>
    <text x="570" y="385" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_market_index</text>
    <text x="570" y="400" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_kimchi_premium</text>
    <text x="570" y="415" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_fear_greed_index</text>
    <text x="750" y="385" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_funding_rate</text>
    <text x="750" y="400" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_correlation</text>
    <text x="750" y="415" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">get_disclosures</text>

    <rect x="850" y="235" width="160" height="90" fill="#FFFDE7" stroke="#F9A825" stroke-width="1" rx="6"/>
    <text x="930" y="260" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#F57F17" text-anchor="middle">AI 분석</text>
    <text x="930" y="280" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">analyze_stock</text>
    <text x="930" y="300" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">(Google Gemini)</text>

    <!-- 심볼 라우터 -->
    <rect x="1030" y="235" width="240" height="195" fill="#ECEFF1" stroke="#546E7A" stroke-width="1" rx="6"/>
    <text x="1150" y="260" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#37474F" text-anchor="middle">심볼 라우터</text>
    <text x="1150" y="285" font-family="Arial, sans-serif" font-size="10" fill="#666666" text-anchor="middle">_resolve_market_type()</text>
    <rect x="1050" y="300" width="200" height="24" fill="#E3F2FD" rx="4"/>
    <text x="1150" y="317" font-family="Arial, sans-serif" font-size="10" fill="#1565C0" text-anchor="middle">005930 → equity_kr</text>
    <rect x="1050" y="330" width="200" height="24" fill="#E8F5E9" rx="4"/>
    <text x="1150" y="347" font-family="Arial, sans-serif" font-size="10" fill="#2E7D32" text-anchor="middle">AAPL → equity_us</text>
    <rect x="1050" y="360" width="200" height="24" fill="#FFF3E0" rx="4"/>
    <text x="1150" y="377" font-family="Arial, sans-serif" font-size="10" fill="#EF6C00" text-anchor="middle">KRW-BTC → crypto</text>
    <text x="1150" y="415" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">시장 자동 감지 + 검증</text>

    <!-- 화살표: MCP 서버 -> 데이터 소스들 -->
    <line x1="250" y1="460" x2="250" y2="510" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="450" y1="460" x2="450" y2="510" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="650" y1="460" x2="650" y2="510" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="850" y1="460" x2="850" y2="510" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="1050" y1="460" x2="1050" y2="510" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="1200" y1="460" x2="1200" y2="510" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 데이터 소스 레이어 -->
    <text x="700" y="505" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#666666" text-anchor="middle">External Data Sources</text>

    <!-- KIS API -->
    <rect x="130" y="520" width="200" height="100" fill="#FF6B00" stroke="#E65100" stroke-width="2" rx="8"/>
    <text x="230" y="550" font-family="Arial, sans-serif" font-size="15" font-weight="bold" fill="#ffffff" text-anchor="middle">KIS API</text>
    <text x="230" y="570" font-family="Arial, sans-serif" font-size="11" fill="#FFE0B2" text-anchor="middle">국내/해외 주식</text>
    <text x="230" y="588" font-family="Arial, sans-serif" font-size="10" fill="#FFCC80" text-anchor="middle">시세 · 보유 · 주문</text>
    <text x="230" y="605" font-family="Arial, sans-serif" font-size="10" fill="#FFCC80" text-anchor="middle">예수금 · 잔고</text>

    <!-- Upbit API -->
    <rect x="350" y="520" width="200" height="100" fill="#093687" stroke="#062563" stroke-width="2" rx="8"/>
    <text x="450" y="550" font-family="Arial, sans-serif" font-size="15" font-weight="bold" fill="#ffffff" text-anchor="middle">Upbit API</text>
    <text x="450" y="570" font-family="Arial, sans-serif" font-size="11" fill="#B3C7E6" text-anchor="middle">암호화폐</text>
    <text x="450" y="588" font-family="Arial, sans-serif" font-size="10" fill="#8BA6CC" text-anchor="middle">시세 · 보유 · 주문</text>
    <text x="450" y="605" font-family="Arial, sans-serif" font-size="10" fill="#8BA6CC" text-anchor="middle">원화마켓</text>

    <!-- Yahoo Finance -->
    <rect x="570" y="520" width="200" height="100" fill="#6001D2" stroke="#4B01A5" stroke-width="2" rx="8"/>
    <text x="670" y="550" font-family="Arial, sans-serif" font-size="15" font-weight="bold" fill="#ffffff" text-anchor="middle">Yahoo Finance</text>
    <text x="670" y="570" font-family="Arial, sans-serif" font-size="11" fill="#D1B3FF" text-anchor="middle">해외 주식</text>
    <text x="670" y="588" font-family="Arial, sans-serif" font-size="10" fill="#B894FF" text-anchor="middle">시세 · 재무제표</text>
    <text x="670" y="605" font-family="Arial, sans-serif" font-size="10" fill="#B894FF" text-anchor="middle">배당 · 밸류에이션</text>

    <!-- Naver Finance -->
    <rect x="790" y="520" width="200" height="100" fill="#03C75A" stroke="#02A74C" stroke-width="2" rx="8"/>
    <text x="890" y="550" font-family="Arial, sans-serif" font-size="15" font-weight="bold" fill="#ffffff" text-anchor="middle">Naver Finance</text>
    <text x="890" y="570" font-family="Arial, sans-serif" font-size="11" fill="#B3F0CC" text-anchor="middle">국내 주식</text>
    <text x="890" y="588" font-family="Arial, sans-serif" font-size="10" fill="#8AE5AB" text-anchor="middle">외국인동향 · 공매도</text>
    <text x="890" y="605" font-family="Arial, sans-serif" font-size="10" fill="#8AE5AB" text-anchor="middle">뉴스 · 목표가</text>

    <!-- Finnhub -->
    <rect x="1010" y="520" width="160" height="100" fill="#1B1B1B" stroke="#000000" stroke-width="2" rx="8"/>
    <text x="1090" y="550" font-family="Arial, sans-serif" font-size="15" font-weight="bold" fill="#ffffff" text-anchor="middle">Finnhub</text>
    <text x="1090" y="570" font-family="Arial, sans-serif" font-size="11" fill="#AAAAAA" text-anchor="middle">해외 주식</text>
    <text x="1090" y="588" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">내부자거래</text>
    <text x="1090" y="605" font-family="Arial, sans-serif" font-size="10" fill="#888888" text-anchor="middle">실적 · 뉴스</text>

    <!-- Binance + CoinGecko -->
    <rect x="1190" y="520" width="170" height="100" fill="#F0B90B" stroke="#C99A09" stroke-width="2" rx="8"/>
    <text x="1275" y="550" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#1E2026" text-anchor="middle">Binance +</text>
    <text x="1275" y="568" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#1E2026" text-anchor="middle">CoinGecko</text>
    <text x="1275" y="590" font-family="Arial, sans-serif" font-size="10" fill="#665100" text-anchor="middle">김치프리미엄</text>
    <text x="1275" y="605" font-family="Arial, sans-serif" font-size="10" fill="#665100" text-anchor="middle">펀딩비 · 프로필</text>

    <!-- 화살표: 데이터소스 -> DB -->
    <line x1="450" y1="620" x2="450" y2="670" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="890" y1="620" x2="890" y2="670" stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 하단: 데이터 저장소 -->
    <rect x="200" y="680" width="400" height="90" fill="#607D8B" stroke="#37474F" stroke-width="2" rx="8"/>
    <text x="400" y="710" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">PostgreSQL</text>
    <text x="400" y="730" font-family="Arial, sans-serif" font-size="12" fill="#CFD8DC" text-anchor="middle">manual_holdings · broker_accounts</text>
    <text x="400" y="748" font-family="Arial, sans-serif" font-size="12" fill="#CFD8DC" text-anchor="middle">stock_aliases · stock_info</text>

    <rect x="700" y="680" width="400" height="90" fill="#D32F2F" stroke="#B71C1C" stroke-width="2" rx="8"/>
    <text x="900" y="710" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">Redis</text>
    <text x="900" y="730" font-family="Arial, sans-serif" font-size="12" fill="#FFCDD2" text-anchor="middle">daily_order_count (주문 제한)</text>
    <text x="900" y="748" font-family="Arial, sans-serif" font-size="12" fill="#FFCDD2" text-anchor="middle">model_rate_limit (API 제한)</text>

    <!-- 하단 범례 -->
    <text x="700" y="830" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">
        Claude → MCP Protocol → FastMCP Server → 7개 데이터 소스 + DB/Redis
    </text>
    <text x="700" y="855" font-family="Arial, sans-serif" font-size="12" fill="#999999" text-anchor="middle">
        심볼 포맷 기반 자동 라우팅 · dry_run 기본 안전 모드 · 비동기 병렬 처리
    </text>
</svg>"""

    def create_routing(self) -> str:
        """심볼 라우팅 다이어그램 (1200x700)"""
        return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="700" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#333333" />
        </marker>
    </defs>

    <!-- 배경 -->
    <rect width="1200" height="700" fill="#ffffff"/>

    <!-- 제목 -->
    <text x="600" y="40" font-family="Arial, sans-serif" font-size="26" font-weight="bold" fill="#1a1a2e" text-anchor="middle">
        지능형 심볼 라우팅 시스템
    </text>
    <text x="600" y="65" font-family="Arial, sans-serif" font-size="14" fill="#888888" text-anchor="middle">
        심볼 포맷에 따라 자동으로 시장과 데이터 소스를 결정
    </text>

    <!-- 입력 심볼 -->
    <rect x="430" y="90" width="340" height="55" fill="#5436DA" stroke="#3d2a9e" stroke-width="2" rx="8"/>
    <text x="600" y="118" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#ffffff" text-anchor="middle">get_quote(symbol, market?)</text>
    <text x="600" y="137" font-family="Arial, sans-serif" font-size="12" fill="#c4b8ff" text-anchor="middle">Claude가 호출하는 MCP 도구</text>

    <!-- 화살표: 입력 -> 라우터 -->
    <line x1="600" y1="145" x2="600" y2="180" stroke="#333333" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 라우터 박스 -->
    <rect x="350" y="185" width="500" height="60" fill="#ECEFF1" stroke="#546E7A" stroke-width="2" rx="8"/>
    <text x="600" y="213" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#37474F" text-anchor="middle">_resolve_market_type(symbol, market)</text>
    <text x="600" y="235" font-family="Arial, sans-serif" font-size="12" fill="#78909C" text-anchor="middle">market 명시 → 검증 | market 생략 → 자동 감지</text>

    <!-- 3개 분기 화살표 -->
    <line x1="450" y1="245" x2="200" y2="310" stroke="#1976D2" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="600" y1="245" x2="600" y2="310" stroke="#388E3C" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="750" y1="245" x2="1000" y2="310" stroke="#F57C00" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 한국주식 감지 -->
    <rect x="60" y="315" width="280" height="130" fill="#E3F2FD" stroke="#1976D2" stroke-width="2" rx="8"/>
    <text x="200" y="345" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#1565C0" text-anchor="middle">equity_kr (한국 주식)</text>
    <rect x="80" y="360" width="240" height="28" fill="#BBDEFB" rx="4"/>
    <text x="200" y="379" font-family="monospace" font-size="13" fill="#1565C0" text-anchor="middle">6자리 영숫자 → 감지</text>
    <text x="200" y="410" font-family="monospace" font-size="12" fill="#666666" text-anchor="middle">005930 → 삼성전자</text>
    <text x="200" y="430" font-family="monospace" font-size="12" fill="#666666" text-anchor="middle">0123G0 → ETF/ETN</text>

    <!-- 미국주식 감지 -->
    <rect x="460" y="315" width="280" height="130" fill="#E8F5E9" stroke="#388E3C" stroke-width="2" rx="8"/>
    <text x="600" y="345" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#2E7D32" text-anchor="middle">equity_us (미국 주식)</text>
    <rect x="480" y="360" width="240" height="28" fill="#C8E6C9" rx="4"/>
    <text x="600" y="379" font-family="monospace" font-size="13" fill="#2E7D32" text-anchor="middle">영문 포함 → 감지</text>
    <text x="600" y="410" font-family="monospace" font-size="12" fill="#666666" text-anchor="middle">AAPL → Apple</text>
    <text x="600" y="430" font-family="monospace" font-size="12" fill="#666666" text-anchor="middle">BRK.B → Berkshire B</text>

    <!-- 암호화폐 감지 -->
    <rect x="860" y="315" width="280" height="130" fill="#FFF3E0" stroke="#F57C00" stroke-width="2" rx="8"/>
    <text x="1000" y="345" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#EF6C00" text-anchor="middle">crypto (암호화폐)</text>
    <rect x="880" y="360" width="240" height="28" fill="#FFE0B2" rx="4"/>
    <text x="1000" y="379" font-family="monospace" font-size="13" fill="#EF6C00" text-anchor="middle">KRW-/USDT- 접두사</text>
    <text x="1000" y="410" font-family="monospace" font-size="12" fill="#666666" text-anchor="middle">KRW-BTC → Bitcoin</text>
    <text x="1000" y="430" font-family="monospace" font-size="12" fill="#666666" text-anchor="middle">USDT-ETH → Ethereum</text>

    <!-- 화살표: 감지 결과 -> 데이터 소스 -->
    <line x1="200" y1="445" x2="200" y2="500" stroke="#1976D2" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="600" y1="445" x2="600" y2="500" stroke="#388E3C" stroke-width="2" marker-end="url(#arrowhead)"/>
    <line x1="1000" y1="445" x2="1000" y2="500" stroke="#F57C00" stroke-width="2" marker-end="url(#arrowhead)"/>

    <!-- 데이터 소스 결과 -->
    <rect x="80" y="505" width="240" height="90" fill="#FF6B00" stroke="#E65100" stroke-width="2" rx="8"/>
    <text x="200" y="535" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">KIS API</text>
    <text x="200" y="555" font-family="Arial, sans-serif" font-size="12" fill="#FFE0B2" text-anchor="middle">한국투자증권</text>
    <text x="200" y="575" font-family="Arial, sans-serif" font-size="11" fill="#FFCC80" text-anchor="middle">+ Naver Finance (펀더멘털)</text>

    <rect x="440" y="505" width="320" height="90" fill="#6001D2" stroke="#4B01A5" stroke-width="2" rx="8"/>
    <text x="600" y="535" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">Yahoo Finance + Finnhub</text>
    <text x="600" y="555" font-family="Arial, sans-serif" font-size="12" fill="#D1B3FF" text-anchor="middle">yfinance (시세/재무)</text>
    <text x="600" y="575" font-family="Arial, sans-serif" font-size="11" fill="#B894FF" text-anchor="middle">Finnhub (뉴스/내부자거래/실적)</text>

    <rect x="860" y="505" width="280" height="90" fill="#093687" stroke="#062563" stroke-width="2" rx="8"/>
    <text x="1000" y="535" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">Upbit API</text>
    <text x="1000" y="555" font-family="Arial, sans-serif" font-size="12" fill="#B3C7E6" text-anchor="middle">업비트 (시세/주문)</text>
    <text x="1000" y="575" font-family="Arial, sans-serif" font-size="11" fill="#8BA6CC" text-anchor="middle">+ Binance/CoinGecko (분석)</text>

    <!-- market 별칭 표 -->
    <rect x="100" y="620" width="1000" height="55" fill="#F5F5F5" stroke="#E0E0E0" stroke-width="1" rx="6"/>
    <text x="120" y="645" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#333333">market 별칭:</text>
    <text x="230" y="645" font-family="monospace" font-size="11" fill="#1565C0">kr, krx, kospi, kosdaq → equity_kr</text>
    <text x="555" y="645" font-family="monospace" font-size="11" fill="#2E7D32">us, nasdaq, nyse, yahoo → equity_us</text>
    <text x="870" y="645" font-family="monospace" font-size="11" fill="#EF6C00">crypto, upbit, krw → crypto</text>
    <text x="600" y="665" font-family="Arial, sans-serif" font-size="11" fill="#999999" text-anchor="middle">market 파라미터를 명시하면 심볼 형식을 해당 시장 규칙으로 검증</text>
</svg>"""


if __name__ == "__main__":
    MCPServerImages("mcp_server").generate()
